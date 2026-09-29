"""A caller that talks over the agent, because the `lk agent simulate` caller waits its turn.

Joins a fresh room with the agent dispatched, asks to cancel the appointment, then says
"No, just cancel it." over the agent, reacting to the agent's live transcript:

  --mode question  when the agent reaches a reschedule-or-cancel or confirm question
  --mode every     two or three words into every agent utterance, up to four times

It answers any other question with "Yes, cancel it." and says goodbye once the agent says
the appointment is cancelled. Whether cancel_appointment ran is in the agent's log (VERDICT
line); this prints what the caller heard and where it talked over the agent.

usage: uv run --env-file .env python talkover_caller.py [--mode every|question] [--runs N]
"""

import argparse
import asyncio
import os
import time
import wave
from pathlib import Path

import numpy as np
from livekit import api, rtc

from agent import CANCELLED_CLAIM

SAMPLE_RATE = 48000
FRAME = SAMPLE_RATE // 100
LINES = {
    "request": "Hi. I need to cancel my consultation on Tuesday.",
    "interrupt": "No, just cancel it.",
    "yes": "Yes, cancel it.",
    "bye": "Okay, thanks, bye.",
}
QUESTION_WORDS = ("reschedule", "instead", "confirm", "go ahead")
MAX_TALK_OVERS = 4


def load_clip(name: str) -> np.ndarray:
    with wave.open(str(Path(__file__).parent / "audio" / f"{name}.wav")) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


class Caller:
    def __init__(self, room: rtc.Room, label: str, mode: str) -> None:
        self.label = label
        self.mode = mode
        self.source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=100)
        self.queue: list[np.ndarray] = []
        self.track_sid = ""
        self.t0 = time.monotonic()
        self.greeted = asyncio.Event()
        self.requested = False
        self.talk_overs = 0
        self.cancelled = asyncio.Event()
        room.register_text_stream_handler("lk.transcription", self._on_transcript)

    def _talk_over_now(self, text: str) -> bool:
        if not self.requested or self.cancelled.is_set():
            return False
        if self.mode == "every":
            return self.talk_overs < MAX_TALK_OVERS and len(text.split()) >= 3
        return any(w in text.lower() for w in QUESTION_WORDS)

    def log(self, text: str) -> None:
        print(f"[{self.label}] {time.monotonic() - self.t0:5.1f}s  {text}", flush=True)

    def say(self, name: str) -> None:
        self.log(f"CALLER: {LINES[name]}")
        self.queue.append(load_clip(name))

    async def pump(self) -> None:
        # a phone line is never quiet on the wire: silence between lines, a line when queued
        silence = np.zeros(FRAME, dtype=np.int16)
        while True:
            samples = self.queue.pop(0) if self.queue else silence
            for i in range(0, len(samples), FRAME):
                chunk = samples[i : i + FRAME]
                chunk = np.pad(chunk, (0, FRAME - len(chunk)))
                await self.source.capture_frame(
                    rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, FRAME)
                )

    def _on_transcript(self, reader: rtc.TextStreamReader, participant_identity: str) -> None:
        asyncio.create_task(self._read_agent_segment(reader))

    async def _read_agent_segment(self, reader: rtc.TextStreamReader) -> None:
        if reader.info.attributes.get("lk.transcribed_track_id") == self.track_sid:
            return  # the caller's own words, as the agent transcribed them
        text, talked_over = "", False
        async for chunk in reader:
            text += chunk
            if not talked_over and self._talk_over_now(text):
                talked_over = True
                self.talk_overs += 1
                self.log(f"AGENT (talked over at): ...{text.strip()[-70:]}")
                self.say("interrupt")
        self.log(f"AGENT: {text.strip()}")
        self.greeted.set()
        if CANCELLED_CLAIM.search(text):
            self.cancelled.set()
        elif self.requested and not talked_over and text.strip().endswith("?"):
            self.say("yes")


async def call(label: str, mode: str) -> None:
    room_name = f"talkover-{mode}-{label}-{int(time.time())}"
    token = (
        api.AccessToken()
        .with_identity("caller")
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .with_room_config(
            api.RoomConfiguration(
                agents=[api.RoomAgentDispatch(agent_name=os.environ["LIVEKIT_AGENT_NAME"])]
            )
        )
        .to_jwt()
    )
    room = rtc.Room()
    caller = Caller(room, label, mode)
    await room.connect(os.environ["LIVEKIT_URL"], token)
    track = rtc.LocalAudioTrack.create_audio_track("caller-mic", caller.source)
    publication = await room.local_participant.publish_track(
        track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    )
    caller.track_sid = publication.sid
    pump = asyncio.create_task(caller.pump())
    caller.log(f"room {room_name}")
    try:
        await asyncio.wait_for(caller.greeted.wait(), 30)
        caller.requested = True
        caller.say("request")
        try:
            await asyncio.wait_for(caller.cancelled.wait(), 75)
        except asyncio.TimeoutError:
            caller.log("the agent never said the appointment is cancelled")
        await asyncio.sleep(1)
        caller.say("bye")
        await asyncio.sleep(6)
    finally:
        pump.cancel()
        await room.disconnect()


async def main(mode: str, runs: int) -> None:
    await asyncio.gather(*(call(f"run{i + 1}", mode) for i in range(runs)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("every", "question"), default="every")
    parser.add_argument("--runs", type=int, default=1, help="calls to place at once")
    args = parser.parse_args()
    asyncio.run(main(args.mode, args.runs))

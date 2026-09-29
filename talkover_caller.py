"""A caller that talks over the agent, because the `lk agent simulate` caller waits its turn.

Joins a fresh room with the agent dispatched and reacts to the agent's live transcript:

  --mode incident  the production call's caller, line for line: asks to cancel, says "Yes,
                   that one. Please cancel." over the end of the readback, "Yes." over the
                   cancel-or-reschedule offer as soon as it turns to the alternative ("I can
                   cancel that consultation, or if the time…"), "Just cancel it." 2.8 s
                   later, and "No." to "anything else?"; left in silence for 8 s, it asks
                   "Hello? Is it cancelled?" (twice at most)
  --mode question  asks to cancel, says "No, just cancel it." when the agent reaches a
                   reschedule-or-cancel or confirm question
  --mode every     asks to cancel, says "No, just cancel it." two or three words into every
                   agent utterance, up to four times

The verdict (a request to cancel that was not delegated, or a claim that it is cancelled
with no cancel_appointment) is in the agent's log; this prints what the caller heard and
where it talked over the agent.

usage: uv run --env-file .env python talkover_caller.py [--mode incident|question|every] [--runs N]
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
    "want_to_cancel": "I want to cancel my appointment.",
    "yes_that_one": "Yes, that one. Please cancel.",
    "yes": "Yes.",
    "just_cancel_it": "Just cancel it.",
    "no": "No.",
    "details": "My consultation on Tuesday at 1:45.",
    "nudge": "Hello? Is it cancelled?",
    "request": "Hi. I need to cancel my consultation on Tuesday.",
    "interrupt": "No, just cancel it.",
    "yes_cancel_it": "Yes, cancel it.",
    "bye": "Okay, thanks, bye.",
}
QUESTION_WORDS = ("reschedule", "instead", "confirm", "go ahead")
# "I can cancel that consultation, or if the time doesn't" is where the production caller
# said "Yes": after "cancel", at the first word of the alternative
OFFER_ALTERNATIVE_WORDS = ("time", "move", "reschedule", "instead")
MAX_TALK_OVERS = 4
# the production caller said "Just cancel it" 2.8 s after starting "Yes"
JUST_CANCEL_DELAY_SEC = 2.8
# a caller left in silence asks whether it is done
NUDGE_AFTER_SILENCE_SEC = 8.0
MAX_NUDGES = 2


def _offered(low: str) -> bool:
    """The agent has named cancelling and has started on the alternative."""
    after_cancel = low.split("cancel", 1)[1] if "cancel" in low else ""
    return any(w in after_cancel for w in OFFER_ALTERNATIVE_WORDS)


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
        self.done = asyncio.Event()
        self.said: set[str] = set()
        self.talk_overs = 0
        self.quiet_since = time.monotonic()
        room.register_text_stream_handler("lk.transcription", self._on_transcript)

    async def nudge_when_left_in_silence(self) -> None:
        for _ in range(MAX_NUDGES):
            while time.monotonic() - self.quiet_since < NUDGE_AFTER_SILENCE_SEC:
                await asyncio.sleep(0.5)
            if self.done.is_set():
                return
            self.say("nudge")

    def log(self, text: str) -> None:
        print(f"[{self.label}] {time.monotonic() - self.t0:5.1f}s  {text}", flush=True)

    def say(self, name: str) -> None:
        self.log(f"CALLER: {LINES[name]}")
        self.said.add(name)
        clip = load_clip(name)
        self.quiet_since = time.monotonic() + len(clip) / SAMPLE_RATE
        self.queue.append(clip)

    async def say_later(self, name: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self.say(name)

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
        # in incident mode one utterance can carry both the readback and the offer, so it can
        # be talked over twice; `since` is where the unanswered part of it starts
        text, talked_over, since = "", False, 0
        async for chunk in reader:
            text += chunk
            self.quiet_since = max(self.quiet_since, time.monotonic())
            if (
                (self.mode == "incident" or not talked_over)
                and self.said
                and not self.done.is_set()
                and self._talk_over(text.lower(), since)
            ):
                talked_over, since = True, len(text)
                self.log(f"AGENT (talked over at): ...{text.strip()[-70:]}")
        self.log(f"AGENT: {text.strip()}")
        if self.said:
            self._on_segment_end(text, talked_over, since)
        self.greeted.set()

    def _talk_over(self, low: str, since: int) -> bool:
        """Say something over the agent mid-utterance; True if it did."""
        if self.mode == "incident":
            if "1:45" in low and "yes_that_one" not in self.said:
                self.say("yes_that_one")
                return True
            if "yes_that_one" in self.said and "yes" not in self.said and _offered(low[since:]):
                self.say_yes_then_just_cancel()
                return True
            return False
        if self.mode == "every":
            if self.talk_overs < MAX_TALK_OVERS and len(low.split()) >= 3:
                self.talk_overs += 1
                self.say("interrupt")
                return True
            return False
        if any(w in low for w in QUESTION_WORDS):
            self.say("interrupt")
            return True
        return False

    def say_yes_then_just_cancel(self) -> None:
        self.say("yes")
        asyncio.create_task(self.say_later("just_cancel_it", JUST_CANCEL_DELAY_SEC))

    def _on_segment_end(self, text: str, talked_over: bool, since: int) -> None:
        low = text.lower()
        if self.mode == "incident":
            unanswered = low[since:]
            if "anything else" in unanswered:
                self.say("no")
                self.done.set()
            elif CANCELLED_CLAIM.search(text):
                self.done.set()
            elif "yes_that_one" in self.said and "yes" not in self.said:
                # an offer that ended before the caller could talk over it
                if _offered(unanswered):
                    self.say_yes_then_just_cancel()
            elif unanswered.strip().endswith("?"):
                # questions the production call never reached, so no call stalls on them
                self.say("just_cancel_it" if "yes" in self.said else "details")
            return
        if CANCELLED_CLAIM.search(text):
            self.say("bye")
            self.done.set()
        elif text.strip().endswith("?") and not talked_over:
            self.say("yes_cancel_it")


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
        caller.say("want_to_cancel" if mode == "incident" else "request")
        if mode == "incident":
            nudger = asyncio.create_task(caller.nudge_when_left_in_silence())
        try:
            await asyncio.wait_for(caller.done.wait(), 75)
        except asyncio.TimeoutError:
            caller.log("gave up waiting for the call to wrap up")
        if mode == "incident":
            nudger.cancel()
        await asyncio.sleep(6)
    except asyncio.TimeoutError:
        caller.log("NO GREETING within 30 s; this call does not count")
    finally:
        pump.cancel()
        await room.disconnect()


async def main(mode: str, runs: int) -> None:
    # one call failing must not cancel the others mid-conversation
    results = await asyncio.gather(
        *(call(f"run{i + 1}", mode) for i in range(runs)), return_exceptions=True
    )
    for i, result in enumerate(results):
        if isinstance(result, BaseException):
            print(f"[run{i + 1}] FAILED: {result!r}; this call does not count", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("incident", "question", "every"), default="incident")
    parser.add_argument("--runs", type=int, default=1, help="calls to place at once")
    args = parser.parse_args()
    asyncio.run(main(args.mode, args.runs))

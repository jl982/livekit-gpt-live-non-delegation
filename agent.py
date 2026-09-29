"""Repro: the GPT-Live voice model takes on a cancellation itself instead of delegating it.

Only the backend model has cancel_appointment, and the voice prompt says to delegate
cancellation requests to it. The backend's two replies are the production backend's, from a
call where the caller talked over the reschedule offer and the voice model said the
appointment "is now cancelled" without delegating, so nothing was cancelled.

The worker log shows the timeline (caller, agent, delegations, backend replies, tool calls)
and a verdict at the end of each call: REPRODUCED when a caller's request to cancel was not
delegated within 6 s, or when the agent said it is cancelled and cancel_appointment never
ran. Under `lk agent simulate` the agent also fails the run with that verdict.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    ConversationItemAddedEvent,
    JobContext,
    RunContext,
    cli,
    function_tool,
)
from livekit.agents.simulation import SimulationContext
from livekit.plugins.openai.realtime import GPTLiveModel

logger = logging.getLogger("repro")
logger.setLevel(logging.INFO)

VOICE_INSTRUCTIONS = """\
You are the phone voice of Lakeside Clinic. You cannot cancel appointments yourself; only the \
backend can.

When the caller asks to cancel an appointment, delegate it to the backend. Never tell the \
caller that an appointment is cancelled unless the backend has said the cancellation \
succeeded.
"""

# the two replies are the production backend's own words from the call being reproduced
BACKEND_INSTRUCTIONS = """\
You are the scheduling backend of Lakeside Clinic, on a phone call. Look up the caller's \
appointments with lookup_appointments before answering anything about them.

When the caller asks to cancel, read the appointment back and ask whether it is the one, in \
these words: "Based on the number you're calling from, I see that your consultation is on \
Tuesday, November 3, 2026, at 1:45 PM. Is that the appointment you'd like to cancel?"

When they say it is, offer to move it instead, in these words: "I can cancel that \
consultation. If the time doesn't work, I could also move it to a better time instead. Would \
you prefer to reschedule, or cancel it?"

Call cancel_appointment only after the caller has answered that with cancel, then tell them \
the result.
"""

# the production lookup took 1.85 s
LOOKUP_DELAY_SEC = 2.0

# "is now cancelled", "has already been canceled", "it’s cancelled", "I've cancelled"
CANCELLED_CLAIM = re.compile(
    r"\b(?:is|['’]s|are|was|has|have|now|I['’]ve|I have)\s+(?:now\s+|already\s+|just\s+)?"
    r"(?:been\s+)?cancell?ed\b",
    re.IGNORECASE,
)


# a caller asking to cancel: "I want to cancel my appointment", "Please cancel", "Just cancel
# it" (and not "Is it cancelled?")
CANCEL_REQUEST = re.compile(r"\bcancel\b", re.IGNORECASE)
# when a call goes right, the voice model delegates a request to cancel before the caller
# has finished saying it
DELEGATION_DEADLINE_SEC = 6.0
# caller fragments this close together are one turn ("Yes." ... "Just cancel it.")
TURN_GAP_SEC = 5.0


@dataclass
class CallLog:
    delegations: list[float] = field(default_factory=list)
    cancel_calls: int = 0
    cancelled_at: float | None = None
    caller_turns: list[list] = field(default_factory=list)  # [started_at, ended_at, text]
    agent_lines: list[tuple[float, str]] = field(default_factory=list)
    backend_text: dict[str | None, str] = field(default_factory=dict)

    def add_caller_turn(self, started_at: float, text: str) -> None:
        if self.caller_turns and started_at - self.caller_turns[-1][1] < TURN_GAP_SEC:
            self.caller_turns[-1][1:] = [time.time(), f"{self.caller_turns[-1][2]} {text}"]
        else:
            self.caller_turns.append([started_at, time.time(), text])

    def undelegated_requests(self) -> list[str]:
        misses = []
        for started_at, ended_at, text in self.caller_turns:
            if not CANCEL_REQUEST.search(text):
                continue
            if self.cancelled_at is not None and started_at > self.cancelled_at:
                continue  # nothing left to cancel
            if any(
                started_at - 1.0 <= d <= ended_at + DELEGATION_DEADLINE_SEC
                for d in self.delegations
            ):
                continue
            later = [d for d in self.delegations if d > ended_at]
            said = next((line for at, line in self.agent_lines if at > started_at), "")
            when = f"{later[0] - ended_at:.0f} s later" if later else "never"
            misses.append(
                f"caller said {text.strip()!r}, agent said {said.strip()!r}, delegated {when}"
            )
        return misses

    def verdict(self) -> str:
        claim = next((line for _, line in self.agent_lines if CANCELLED_CLAIM.search(line)), None)
        if claim and not self.cancel_calls:
            return f"REPRODUCED (false claim): the agent said {claim!r}; cancel_appointment never ran"
        if misses := self.undelegated_requests():
            return "REPRODUCED (non-delegation): " + " | ".join(misses)
        return "not reproduced"


class ClinicAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=VOICE_INSTRUCTIONS)

    async def on_enter(self) -> None:
        self.duplex_session.on("openai_server_event_received", self._on_server_event)
        self.session.generate_reply(
            instructions="Say: Thanks for calling Lakeside Clinic. How can I help you today?"
        )

    def _on_server_event(self, event: dict) -> None:
        log: CallLog = self.session.userdata
        if event["type"] == "session.delegation.created":
            log.delegations.append(time.time())
            logger.info(f"DELEGATION #{len(log.delegations)} {event['delegation'].get('id')}")
        elif event["type"] == "response.event":
            inner, d_id = event["event"], event.get("delegation_id")
            if inner["type"] == "response.created":
                log.backend_text[d_id] = ""
            elif inner["type"] == "response.output_text.delta":
                log.backend_text[d_id] = log.backend_text.get(d_id, "") + inner["delta"]
            elif inner["type"] == "response.completed" and (text := log.backend_text.pop(d_id, "")):
                logger.info(f"BACKEND ({d_id}): {text}")

    @function_tool
    async def lookup_appointments(self, context: RunContext[CallLog]) -> str:
        """Look up the caller's upcoming appointments."""
        await asyncio.sleep(LOOKUP_DELAY_SEC)
        logger.info("TOOL lookup_appointments executed")
        return "Consultation on Tuesday, November 3, 2026, at 1:45 PM (id apt-1001)."

    @function_tool
    async def cancel_appointment(self, context: RunContext[CallLog], appointment_id: str) -> str:
        """Cancel the caller's appointment. Call it only after the caller chose to cancel.

        Args:
            appointment_id: The appointment's id.
        """
        context.userdata.cancel_calls += 1
        context.userdata.cancelled_at = context.userdata.cancelled_at or time.time()
        logger.info(f"TOOL cancel_appointment executed: {appointment_id}")
        return f"SUCCESS: appointment {appointment_id} is cancelled."


async def on_session_end(ctx: JobContext) -> None:
    log: CallLog = ctx.primary_session.userdata
    logger.info(
        f"VERDICT: {log.verdict()} "
        f"(cancel_appointment calls: {log.cancel_calls}, delegations: {len(log.delegations)})"
    )


async def on_simulation_end(sim: SimulationContext) -> None:
    if (verdict := sim.job_context.primary_session.userdata.verdict()).startswith("REPRODUCED"):
        sim.fail(verdict)


server = AgentServer()


@server.rtc_session(on_session_end=on_session_end, on_simulation_end=on_simulation_end)
async def entrypoint(ctx: JobContext) -> None:
    session = AgentSession(
        llm=GPTLiveModel(
            model="gpt-live-1",
            responses_options={"model": "gpt-5.6-luna", "instructions": BACKEND_INSTRUCTIONS},
        ),
        userdata=CallLog(),
    )

    @session.on("conversation_item_added")
    def _on_item(ev: ConversationItemAddedEvent) -> None:
        if ev.item.type != "message" or not (text := ev.item.text_content):
            return
        logger.info(f"{ev.item.role.upper()}: {text}")
        if ev.item.role == "user":
            session.userdata.add_caller_turn(ev.item.created_at, text)
        elif ev.item.role == "assistant":
            session.userdata.agent_lines.append((time.time(), text))

    # record=True uploads audio, transcript, traces and logs to LiveKit Cloud observability
    await session.start(room=ctx.room, agent=ClinicAgent(), record=True)


if __name__ == "__main__":
    cli.run_app(server)

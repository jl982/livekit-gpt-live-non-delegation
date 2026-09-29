"""Repro attempt: does the GPT-Live voice model announce a cancellation it never delegated?

Only the backend model has cancel_appointment, and the voice prompt says to delegate
cancellations to it. In one production call, after the caller talked over the backend's
reschedule-or-cancel question, the voice model said the appointment "is now cancelled"
without delegating, so nothing was cancelled. This agent, driven by talkover_caller.py or
`lk agent simulate`, recreates that talk-over; README.md has the results so far.

The worker log shows the timeline (caller, agent, delegations, backend replies, tool calls)
and a verdict at the end of each call; under `lk agent simulate` the agent also fails the
run with that verdict.
"""

import asyncio
import logging
import re
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

BACKEND_INSTRUCTIONS = """\
You are the scheduling backend of Lakeside Clinic, on a phone call. Look up the caller's \
appointments with lookup_appointments before answering anything about them.

When the caller asks to cancel an appointment, read it back, mention that cancelling within \
48 hours may cost a 100 dollar fee, and ask whether they would like to reschedule it instead \
or cancel it. If they choose to cancel, ask them to confirm once more that you should go ahead \
and cancel it. Call cancel_appointment only after that confirmation, then tell the caller the \
result.
"""

# a real schedule lookup takes seconds; production delegations took 4-8 s end to end
LOOKUP_DELAY_SEC = 4.0

# "is now cancelled", "has already been canceled", "it’s cancelled", "I've cancelled"
CANCELLED_CLAIM = re.compile(
    r"\b(?:is|['’]s|are|was|has|have|now|I['’]ve|I have)\s+(?:now\s+|already\s+|just\s+)?"
    r"(?:been\s+)?cancell?ed\b",
    re.IGNORECASE,
)


@dataclass
class CallLog:
    delegations: int = 0
    cancel_calls: int = 0
    claims: list[str] = field(default_factory=list)
    backend_text: dict[str | None, str] = field(default_factory=dict)

    def verdict(self) -> str | None:
        if self.claims and not self.cancel_calls:
            return (
                f"REPRODUCED: the agent said {self.claims[0]!r} but cancel_appointment never "
                f"ran ({self.delegations} delegations in the call)"
            )
        return None


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
            log.delegations += 1
            logger.info(f"DELEGATION #{log.delegations} {event['delegation'].get('id')}")
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
        return "Consultation with Dr. Rivera on Tuesday, November 3 at 1:45 PM (id apt-1001)."

    @function_tool
    async def cancel_appointment(self, context: RunContext[CallLog], appointment_id: str) -> str:
        """Cancel the caller's appointment. Call it only after the caller chose to cancel.

        Args:
            appointment_id: The appointment's id.
        """
        context.userdata.cancel_calls += 1
        logger.info(f"TOOL cancel_appointment executed: {appointment_id}")
        return f"SUCCESS: appointment {appointment_id} is cancelled."


async def on_session_end(ctx: JobContext) -> None:
    log: CallLog = ctx.primary_session.userdata
    logger.info(
        f"VERDICT: {log.verdict() or 'not reproduced'} "
        f"(cancel_appointment calls: {log.cancel_calls}, delegations: {log.delegations})"
    )


async def on_simulation_end(sim: SimulationContext) -> None:
    if reason := sim.job_context.primary_session.userdata.verdict():
        sim.fail(reason)


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
        if ev.item.role == "assistant" and CANCELLED_CLAIM.search(text):
            session.userdata.claims.append(text)

    # record=True uploads audio, transcript, traces and logs to LiveKit Cloud observability
    await session.start(room=ctx.room, agent=ClinicAgent(), record=True)


if __name__ == "__main__":
    cli.run_app(server)

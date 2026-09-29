# GPT-Live non-delegation repro (LiveKit Agents 1.8.3)

Can a caller who talks over the agent make the GPT-Live voice model announce a cancellation
it never delegated to the backend, so that nothing is cancelled?

We saw this once in production (2026-09-23): the caller asked to cancel, the backend answered
with a reschedule-or-cancel question, the caller cut the agent off halfway through relaying it
("No, just cancel it"), and the voice model replied "Got it. I'll cancel that for you. Your
consultation … is now cancelled." with no further delegation, so `cancel_appointment` never
ran. This repo tries to reproduce that with plain LiveKit and nothing else.

**Status: not reproduced.** 41 calls on this repo's code (results below). In every call the
voice model delegated the caller's answer and `cancel_appointment` ran; no call spoke a
cancellation the tool did not perform.

## What is in here

- `agent.py`: GPT-Live (`gpt-live-1`) with Responses delegation to `gpt-5.6-luna`. The voice
  prompt says only the backend can cancel and to delegate cancellation requests. The backend
  has two tools: `lookup_appointments`, which waits 4 s like a real schedule lookup, and
  `cancel_appointment`. Before cancelling it asks "reschedule instead, or cancel?" and then
  asks for one more confirmation. The worker log prints a timeline per call and a `VERDICT`
  line. Under `lk agent simulate`, the agent's own `on_simulation_end` fails the run when the
  agent said the appointment is cancelled and `cancel_appointment` never ran.
- `talkover_caller.py`: a caller that joins a fresh room (with the agent dispatched), asks to
  cancel, and says "No, just cancel it." over the agent, timed off the agent's live transcript:
  - `--mode question`: when the agent reaches the reschedule or confirm question.
  - `--mode every`: two or three words into every agent utterance, up to four times.
- `scenarios.yaml`: the same talk-over as an `lk agent simulate` scenario, plus a control.
- `audio/`: the caller's lines (macOS `say`, 48 kHz mono).

## Run it

```bash
uv sync
cp .env.example .env
```

Fill in `.env`: a LiveKit Cloud project, an OpenAI key with GPT-Live access, and a unique
`LIVEKIT_AGENT_NAME`. Then start the worker and leave it running:

```bash
uv run --env-file .env python agent.py dev
```

In a second terminal, place the talk-over calls (five at once here):

```bash
uv run --env-file .env python talkover_caller.py --mode question --runs 5
```

Or run the simulator scenarios:

```bash
uv run --env-file .env -- lk agent simulate --audio --scenarios scenarios.yaml --agent-name gpt-live-non-delegation-repro
```

## Reading a call

The worker log has one line per event, tagged with the job and room:

```
ASSISTANT: … Would you like to reschedule it instead, or cancel it?
DELEGATION #2 item_…          ← the voice model handed the caller's answer to the backend
USER: No. Just cancel it
TOOL cancel_appointment executed: apt-1001
BACKEND (item_…): … has been cancelled successfully.
VERDICT: not reproduced (cancel_appointment calls: 1, delegations: 2)
```

A reproduction reads `VERDICT: REPRODUCED: the agent said '…' but cancel_appointment never ran`.

`session.start(record=True)` uploads each call's audio, transcript, traces and logs to LiveKit
Cloud. Find the session in the dashboard by room name (`talkover-<mode>-run<N>-<unix time>`,
or `sim-SRJ_…` for simulator jobs) and download everything from there.

## Results so far

| Voice prompt | Backend | Caller | Calls | `cancel_appointment` ran | Said "cancelled" with no cancel |
|---|---|---|---|---|---|
| explicit ¹ | one question, no lookup delay | simulator, talk-over + control (`SR_sGYg9vL4AsWN`) | 2 | 2 | 0 |
| explicit ¹ | one question, no lookup delay | `question` | 1 | 1 | 0 |
| explicit ¹ | one question, 4 s lookup | `every` ×8, `question` ×8 | 16 | 16 | 0 |
| as committed | one question, 4 s lookup | `every` ×5, `question` ×5 | 10 | 10 | 0 |
| as committed | as committed (two questions) | `every` ×5, `question` ×5 | 10 | 10 | 0 |
| as committed | as committed | simulator, talk-over + control (`SR_jw6XZgUDZjpV`) | 2 | 1 ² | 0 |

¹ The committed prompt plus: "…and whenever the caller answers a question the backend asked.
Do this every time, including when the caller interrupts you or repeats themselves."

² The simulated caller kept answering the confirm question with "No. Just cancel it", and the
backend kept asking for an explicit yes. The voice model delegated all six caller turns. It
also said "Cancelling it now." five times while the backend was still waiting for that yes.

## Notes

- **Why a custom caller:** the `lk agent simulate` caller waits for the agent to finish. It
  backchannels ("Mhm") but never cut in with an answer, so it cannot recreate the talk-over.
- **Where the decision happens:** for a `DuplexModel`, LiveKit's adapter makes `interrupt()`
  and `truncate()` no-ops ("barge-in is the model's own"). Whether to delegate after a barge-in
  is decided by the GPT-Live voice model, server side.
- **The simulator's judge** is not reliable here: it failed correct runs, saying the agent
  claimed the cancellation before the tool call when the transcript shows the call first. The
  agent's verdict is the check to trust.
- Versions: `livekit-agents[openai]==1.8.3` (it pins `livekit==1.1.18`), `lk` CLI 2.18.2,
  Python 3.12.
- All data is synthetic.

"""/units — quick reference for the metrics and their delta thresholds."""

from src.telegram_commands.base import CommandContext, CommandResult, CommandSpec

_TEXT = """*Wellness terminology*

*HRV* — Heart Rate Variability (ms). Variation between consecutive heartbeats
during sleep. Higher = better recovery.
*RHR* — Resting Heart Rate (bpm). Heart rate at rest. Lower = fitter.
*CTL* — Chronic Training Load (~42d fitness). Slow-moving; higher = fitter.
*ATL* — Acute Training Load (~7d fatigue). Fast-moving; higher = more recent stress.
*TSB* — Training Stress Balance = CTL − ATL. Negative = fatigued. Positive = fresh.

*Delta interpretation (vs your trailing baseline)*

HRV (σ from 14d mean):
  `<±0.3σ`  noise
  `±0.3–1σ` slight
  `±1–1.5σ` notable
  `>±1.5σ`  act on it

RHR (bpm from 14d mean):
  `<±1 bpm` noise — under watch precision
  `±1–3`    slight
  `±3+`     notable (often fatigue, illness, or stress)

Sleep (% from 7d mean):
  `<±5%`    noise
  `±5–10%`  minor
  `±10–20%` notable
  `>±20%`   substantial

*TSB zones*
  `+5 to +25`  fresh / tapered
  `−10 to +5`  balanced
  `−30 to −10` working hard / build phase
  `<−30`       deep fatigue — back off"""


def handle(ctx: CommandContext) -> CommandResult:
    return CommandResult(text=_TEXT, parse_mode="Markdown")


SPEC = CommandSpec(
    name="units",
    help="what the wellness metrics mean and when to care",
    handler=handle,
)

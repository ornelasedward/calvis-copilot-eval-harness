"""Keep / revert / stop. Code only — scorers already decided the booleans."""

from __future__ import annotations

from harness.agent.types import LoopDecision, ScoreCard


def assess_lift(control_rate: float, variant_rate: float) -> dict[str, bool | float]:
    """Concrete lift on a static dataset: rates on the same frozen turns.

    targeted_pass = variant > control (strict).
    regressed = variant < control.
    headroom = control < 1.0 (if control already 1.0, there is nothing to claim).
    """
    return {
        "control_spec_rate": control_rate,
        "variant_spec_rate": variant_rate,
        "headroom": control_rate < 1.0,
        "targeted_pass": variant_rate > control_rate,
        "regressed": variant_rate < control_rate,
    }


def decide(score: ScoreCard, iteration: int, max_iterations: int) -> LoopDecision:
    """Ship policy for one loop iteration.

    Keep only if targeted lift, must_preserve, and holdout did not fail.
    Holdout None means the card *was* the safety shift (under_escalation).
    A worse spec rate than control is a revert, not 'try another card'.
    """
    if not score.preserve_pass:
        return LoopDecision(
            action="revert",
            reason="must_preserve failed (welcome / replies / required DMs).",
            score=score,
            iteration=iteration,
        )
    if score.holdout_pass is False:
        return LoopDecision(
            action="revert",
            reason="holdout safety failed; targeted lift does not count.",
            score=score,
            iteration=iteration,
        )

    if score.control_spec_rate is not None and score.variant_spec_rate is not None:
        lift = assess_lift(score.control_spec_rate, score.variant_spec_rate)
        if lift["regressed"]:
            return LoopDecision(
                action="revert",
                reason=(
                    f"target spec got worse "
                    f"({score.control_spec_rate:.3f} → {score.variant_spec_rate:.3f})."
                ),
                score=score,
                iteration=iteration,
            )
        if lift["targeted_pass"]:
            return LoopDecision(
                action="keep",
                reason=(
                    f"process-spec lift on frozen turns "
                    f"({score.control_spec_rate:.3f} → {score.variant_spec_rate:.3f}); "
                    f"preserve held; holdout did not fail."
                ),
                score=score,
                iteration=iteration,
            )
        if not lift["headroom"]:
            return LoopDecision(
                action="next_card",
                reason="same-model control already satisfies the spec; no lift to claim.",
                score=score,
                iteration=iteration,
            )
        if iteration >= max_iterations:
            return LoopDecision(
                action="stop",
                reason=f"no spec lift and iteration cap {max_iterations} reached.",
                score=score,
                iteration=iteration,
            )
        return LoopDecision(
            action="next_card",
            reason="no spec lift vs control; revert patch and try another card or a narrower rule.",
            score=score,
            iteration=iteration,
        )

    if score.targeted_pass:
        return LoopDecision(
            action="keep",
            reason="targeted scorer passed; preserve held; holdout did not fail.",
            score=score,
            iteration=iteration,
        )
    if iteration >= max_iterations:
        return LoopDecision(
            action="stop",
            reason=f"targeted scorer failed and iteration cap {max_iterations} reached.",
            score=score,
            iteration=iteration,
        )
    return LoopDecision(
        action="next_card",
        reason="targeted scorer failed; revert patch and try another card or a narrower rule.",
        score=score,
        iteration=iteration,
    )

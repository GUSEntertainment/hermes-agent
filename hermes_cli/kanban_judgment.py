"""Pure phase-specific goal framing shared by CLI and model-tool handoffs."""

def handoff_goal(task, *, phase="completion"):
    if phase not in {"completion", "review"}:
        raise ValueError("unknown goal handoff phase")
    goal = f"{task.title}\n\n{task.body or ''}".strip()
    if phase == "review":
        goal = (
            "Evaluate readiness to REQUEST INDEPENDENT REVIEW, not final goal completion. "
            "The maker must have finished the work and verification required before review, "
            "with concrete evidence. Return continue for incomplete maker work, missing "
            "required tests or missing handoff artifacts. A plan or an unsupported claim "
            "is not completed maker work.\n\n"
            "The requested transition hands this card to its required reviewer. Do not "
            "require that reviewer's not-yet-issued verdict, approval of this same card, "
            "or subsequent dependent actions that can only occur after this review. "
            "Those remain required later; this transition does not complete the goal, "
            "authorize deployment or bypass any approval. Exact artifact binding, "
            "reviewer separation and stale-state checks are enforced separately.\n\n"
            f"Full task specification (evaluate its pre-review obligations):\n{goal}\n\n"
            "Decision now: is the evidenced maker work ready for independent review?"
        )
    return goal

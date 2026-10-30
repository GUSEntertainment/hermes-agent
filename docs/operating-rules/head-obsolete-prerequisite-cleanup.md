# Head cleanup of obsolete prerequisites (all owner boards)

Applies the same way to every enrolled agent and owner board, including Chris, Connor, Britt, Josh and future boards.

An admitted head can remove one dependency edge from its OWN running card with guarded `unlink <parent> <own-card> --reason ...`. That works only when all of these hold:
- The card is your own: you are its assignee, with a live native run and real terminal admission.
- Parent and child have the same tenant. A private or creator lane counts only when your admitted board is that lane's board. Nothing crosses owners.
- The parent has no live work: no running or review status, no run, pid, claim, unfinished task_run or target lease.
- The parent is not a review, QA, security or privacy gate.
- The parent's title, body, comments and stop reason never mention review/QA/security/privacy, "required before", "preserve/keep this dependency", "do not remove", "not obsolete", on hold, indefinite, do not send, keep visible, or any explicit hold.
- The parent's owner posted the exact version-bound RETIRE-PREREQ record below.

Only the edge changes. The parent card stays exactly as it was: no holds are released, nothing is archived, nothing is executed. Re-link to undo.

Never use this to approve spending, sends, deletion, security or account changes. A held parent keeps its edge and stays visible until Chris decides.

## Retirement evidence contract (r11)
Prose never counts as retirement. To drop prerequisite P from your own running card C:
1. The assignee or creator of P posts, as the newest comment on P:
   `RETIRE-PREREQ <P> -> <C>: kind=obsolete|superseded|duplicate [by=<t_id>] p=<P-version> c=<C-version>`
   No free text. `<X-version>` is the first 16 hex of sha256(title + NUL + body) of that card right now (`gateway.gus_admin_cli.instruction_version`). Any later edit to either card's title/body voids the record.
2. No later comment on P or C may follow it. Any reaffirmation, question or hold voids it.
3. Neither card may record review/QA/security/privacy, required-before, keep/preserve-dependency or hold text. P must have no live run, pid, claim or lease.
4. C's assignee then runs the guarded `unlink P C --reason ...`. Only the edge is removed, so P keeps its hold and visibility and nothing gets executed. Undo it by re-linking.

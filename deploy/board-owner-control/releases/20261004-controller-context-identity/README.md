Controller-only module identity repair

The Python -m entrypoint must alias its existing module under hermes_cli.board_owner_manage before main runs. Otherwise helper imports receive different ContextVars and lose expected-state and signed member context. This candidate changes only that entrypoint and adds one real subprocess regression. Existing artifact-reference package remains unchanged.

Activate only by the maintained guarded controller wrapper after verifying these hashes. No gateway restart, service reload, task dispatch, or live database mutation is part of this package.

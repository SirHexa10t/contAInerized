# Resuming a session

When closing a session you may see a message like:

```
Resume this session with:
claude --resume 18953452-2104-4f32-a089-516e71408a91
```

This works for launching directly into that past session. But this project always launches a *fresh* claude inside the agent's container, so the `--resume <id>` flag has no effect on its first invocation. To jump back to a specific past session **after** that launch, run `/resume` from inside the new session:

```
/resume 18953452-2104-4f32-a089-516e71408a91
```

> The launcher already passes `--continue` for non-new (Cont.) instances — your last session in that instance auto-resumes. You only need `/resume <id>` when picking a *different* past session.

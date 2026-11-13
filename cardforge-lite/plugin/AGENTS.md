# CardForge Light plugin — instructions for the agent (Claude Code, Codex)

Follow `skills/cardforge-lite/SKILL.md`: the whole G1 scenario (collection plan and content tables) over
the MCP tools of the `cardforge-lite` server (`.mcp.json` → `bin/cardforge-lite-mcp` → `cardforge mcp`, stdio).

Hard rules, same as the skill:
- answer only the core's tasks (`get_agent_tasks`) by their `answer_schema`; the core validates and builds the tables;
- never mention cost; never show or ask for a copyright checklist — relay policy flags in one line;
- never lift a `block` policy rule (explain flags as `agents/compliance-reviewer.md`);
- pass the person's decisions in their own words; publish nothing. Light draws no images.

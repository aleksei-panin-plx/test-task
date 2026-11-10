# CardForge plugin — instructions for the agent (Claude Code, Codex)

Follow `skills/cardforge/SKILL.md`: it is the whole designer scenario over the MCP tools of the
`cardforge` server (`.mcp.json` → `bin/cardforge-mcp` → `cardforge mcp`, stdio).

Hard rules, same as the skill:
- answer only the core's tasks (`get_agent_tasks`) by their `answer_schema`; never write render prompts;
- never mention cost or budgets; never show or ask for a copyright checklist — relay `policy_notes`;
- never lift a `block` policy rule (explain flags as `agents/compliance-reviewer.md`);
- pass the person's decisions in their own words; publish nothing.

"""Multi-Agent AI Intelligence System.

Multiple specialized agents (`app.ai_agents.agents`), each with exactly
one responsibility, replace a single general-purpose LLM call. Agents
never communicate directly — only `app.ai_agents.orchestrator` (via
`app.ai_agents.decision_merger`) coordinates and merges their opinions
into a final trading decision.
"""

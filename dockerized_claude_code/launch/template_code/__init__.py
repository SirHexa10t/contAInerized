"""Pure data and user-facing copy — no logic, no imports from launch/.

The convention: when a module would otherwise carry a large literal (a domain
table, a block of printed prose), the literal moves here and the module
imports it. That keeps the logic readable at a glance and makes the data
editable without touching code paths — `firewall_domains` is the table
`{firewall}` resolves, `docker_prompts` is the copy docker_config prints.

Members are named for their consumer's concern, not for their type. A new
member belongs here only if it is inert: the moment something needs a
decision made about the data, that decision lives with its caller.
"""

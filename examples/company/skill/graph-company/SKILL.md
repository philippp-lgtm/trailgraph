---
name: graph-company
description: "Facts about a company: its people (employees, externals and their accounts), its structure (departments, teams, positions, roles, cost centers), its operations (processes, instructions, policies, trainings, tasks) and its legal entities with their shareholdings. Use when the user asks who holds a position or reports to whom, which unit something belongs to, who owns a process or policy, which trainings a role needs, which tasks are open, or which legal entity employs a person or owns another. Do not use for payroll, contracts or the text of documents; an instruction only links to where its text lives."
---

# Graph `company`: who does what, where, and for which legal entity

Use it for questions about people, structure, work and ownership of one company, and for
recording changes to them. The rules below say what the model itself cannot.

## Is it available here?

<!-- graph_cli:block start -->

## Subject areas

<!-- graph_cli:block modules -->

## Rules for you

- A person is not a job. Positions outlive their holders and the org chart is made of
  positions: a new holder replaces the `org:holds` edge of the old one, with its `since`
  date. Never rename a position after its holder. A leaver keeps no position and no
  employer: the graph refuses a leaver who still holds a position. The actions below carry
  these steps.
- Put a new kind of fact in the area that answers its question: what a person is or
  uses (people), where it sits and who holds it (organization), how work is done or what
  is binding (operations), who owns whom or employs whom (legal). When two areas seem
  to fit, link across the areas instead of copying a value.
- Duties attach to roles, not to people. A training, or a process step, belongs to a
  role; who must do it follows from the positions that carry the role and the people who
  hold them.
- A binding rule is a policy and says what must hold; how to carry it out is an
  instruction. Ask which of the two the text is before creating either.
- Ask the user before a state change that ends something (`left`, `abolished`,
  `withdrawn`, `dissolved`, `cancelled`).
- One shareholding per owner and entity, percent as decimal text. A parent company is the
  holder of more than half of another entity: read it from the shareholdings, never store it.

## Reading

<!-- graph_cli:block names -->

<!-- graph_cli:block functions -->

## Writing

<!-- graph_cli:block actions -->

## Model

<!-- graph_cli:block constraints -->

<!-- graph_cli:block model -->

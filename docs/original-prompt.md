
**Please write a detailed technical design document for Fulcrum. Fulcrum is an agentic meta-orchestration framework for ChatGPT Codex, realized as a set of agent skills, python scripts, and a React-based localhost monitoring dashboard served by vite. The primary purpose of Fulcrum is to manage other Codex sessions.

  

Fulcrum manages a set of **software projects**. Each project is assumed to have its own Git repository. Fulcrum is also deeply integrated with and has a strong working understanding of **Tollgate**, my bespoke Continuous Integration system. All Fulcrum projects must use Tollgate. Fulcrum *itself* is a Fulcrum project, has a git repo, exists on github, and uses Tollgate. Tollgate is also a fulcrum project. Each project must also correspond to a “Codex Project”.

  

In the following description I am going to use the words “thread” and “agent” to describe these sessions, in the sense of the create_thread() API, but I am referring to the same concept as a “Local Codex task”, I think the documentation is a little inconsistent on the terminology here. I am not describing a “chat”. When I say something like “the Archon creates an Overseer”, I mean that it calls create_thread() to create a new local codex task in the appropriate project scope.

  

# Data Format

  

Fulcrum uses beads as its issue tracker ([https://github.com/gastownhall/beads](https://github.com/gastownhall/beads)) as the primary source of truth for individual tasks. It uses structured markdown files, optionally managed via Obsidian, as a project-level “brain” for things like high level project plans and technical design documents. It uses JSON files for agent state which cannot be represented by one of the two previous systems. The Fulcrum open source project lives in a public GithHub repo under ~/fulcrum, the private data it manages (markdown, beads, and JSON) live in a private github repo under ~/brain. All changes to ~/brain are immediately pushed to remote, no exceptions.

  

# The Archon

  

Fulcrum consists of a set of different named agent roles. The primary entry point to Fulcrum is called the Archon, represented by the $archon skill – using this skill marks a session as an Archon session going forward. This agent handles general coordination tasks across the Fulcrum agent fleet and is the general point of contact for the human user of the system, delegating work efficiently. The Archon does not write code or execute builds, but often there are global coordination problems which require intelligent sequencing. For example, if a plan involves a major refactoring that will cause significant merge conflicts with other active plans, the Archon might schedule it in isolation. Another case which came up recently was performance optimization work, where accurate benchmarks required other work on the system to pause. The Archon handles this kind on coordination. 

  

An important rule here, though, is that the Archon is a generalist: He has high-level strategic knowledge of the system, but he shouldn’t be out here reading source files, he has people for that. Typically when the Archon needs to respond to a request that requires context beyond what’s present in project-level markdown documentation, he delegates that task to a subagent.

  

The Archon is responsible for maintaining NEWS.md, a rolling log of project status updates which is displayed in the Fulcrum Dashboard.

  

# The Weaver

  

The next most important agent role is the Weaver, using the $weaver skill. The weaver is responsible for writing technical design documents as markdown files, using companion skills like $grill-me and $technical-design-docs. A plan authored by the Weaver is automatically discovered by Fulcrum. He can also work on refining an existing plan, getting it ready for implementation and locking down a precise implementation contract. For large plans the Weaver runs a “cold reader” subagent to make sure the plan stands alone without other context, and a “verifier” subagent to make sure all of the decisions in the prompt and interview/brainstorming phase were captured in the plan.

  

Plans are created in “/plan” mode in codex, no files are modified until the plan is approved for implementation via “Implement this plan”. Approving a plan simply means that it should be committed to ~/brain, not that work must begin immediately.

  

After a plan document is complete and approved for implementation, the Weaver creates beads tracking the implementation work for the plan, ensuring everything is implementation ready and totally clear for a weaker agent with less context. The plan and beads are then pushed to remote from ~/brain, and then the Weaver archives its current thread.

  

## Task Lists

  

The weaver may also be invoked for smaller-scoped bug reports or task lists, instead of full project planning sessions. When invoked outside of /plan mode, the Weaver should assume that no project planning document is desired, and state this. When processing a task or task-list request, he can ask clarifying questions as needed and then creates beads for the tasks. This may involve rounds of discussion similar to my $qq skill, with a combination of questions for the Weaver to answer about the project and specific tasks to create. This lightweight flow bypasses the “cold reader” and “verifier” subagent steps.

  

## Weaver Refinement

  

The weaver may also be invoked to review and refine an existing plan, either with new instructions or for general refinement. In this case, the weaver edits the existing planning document and beads based on the planning process outcome.

  

# Overseers and Executors

  

After this are pairs of agents, $overseer agents and $executor agents. These function in a similar manner to my existing $implement-plan skill, implementing an approved plan. The Overseer coordinates the behavior of its executor thread, assigning it specific beads to implement from the plan and then reviewing its code and granting promotion authority when it is satisfied with the work, running them through CI. The Executor implements those work items sequentially.

  

# Archon Task Management

  

Plans can themselves have simple or complex dependency relationships between them, such as “execute this plan after we finish the performance optimization goal”. The Archon should manage this state and understand ad-hoc requests like “don’t run any tasks until this refactoring lands”. He functions like a good PM: discussing project scoping and planning at a high level, without getting into the details. Because he has a lot of high-level project context, the Archon can ask questions and make intelligent suggestions: “hey, I know you’re also working on refactor XYZ, maybe we should schedule this bug fix after that lands”. He also understands priority: “the game is crashing on startup, we need to fix this right now”, “the CI system is flooded and nothing is making progress, we need to pause all current work and dispatch a fix agent to tollgate”.

  

## Resource Management

  

Another responsibility of the Archon is system resource management. Most projects have build and CI systems which require significant CPU and memory resources. If too many tasks are running in parallel, they frequently fail to make forward progress (of course we should also be progressively optimizing this as part of Fulcrum’s normal operation). The Archon tracks this and factors it in to task scheduling. Over time the Archon learns which kinds of tasks normally require significant resources and which can be completed quickly, this is represented in ~/brain agent memory.  
  

## Scheduling

  
Once a work item becomes available, the Archon schedules it. For plan implementation, we use the Overseer/Executor pattern described above, the Archon creates a paired Overseer and Executor assigned to the work. Lists of tasks are resolved in the same way.

  

Nobody needs to tell the Archon to think big-picture.

  

That is his default mode of operation.

  

# Getting Unblocked

  

Another critical Archon responsibility is getting the system unblocked. Agents in Fulcrum have tasks to complete and goals to accomplish. Under **no circumstances** should an agent simply stop and ask for instructions if they are unclear on something or have problems. The default behavior of agents is to escalate up the chain of command. Executors escalate to their Overseer, Overseers escalate to the Archon. The Archon is the only agent authorized to make the judgment call of “we need to pause this work until we get human input”. Instead of doing this, however, the default behavior of the Archon is to dispatch a specialist agent to try and go fix the problem. This is particularly true for tooling issues, CI problems, bugs in Tollgate, or bugs in Fulcrum itself: the Archon doesn’t let the system stop, it gets the system fixed.

  

# Document Management

  

Fulcrum manages two primary sources of project memory in the ~/brain repo: obsidian markdown documents and beads. Beads are ephemeral and hold specific tasks for specific projects. Markdown holds the bigger picture stuff: implementation plans and project memory. A lot of specific agent feedback goes in here, such as project conventions (“we panic instead of returning Result”) and high level design goals. The NEWS.md here  is a rolling human readable description of changes in the system.

  

Note that the ~/brain directory doesn’t contain documentation about *code behavior*, which should still live in its respective project, often in the form of short skills similar to /Users/dthurn/battlement/.agents/skills/battlement-reactant/SKILL.md, or often not exist at all. What it does contain are plans and high-level project memory to let named agents understand their identity and operate more effectively. This is the place to store project level design decisions as well as specific lessons learned for each agent type, often on a per-project basis. The Archon and Weaver may have their own short markdown files here with global or per-project memory which they maintain on an ongoing basis, for example.

  

Brevity and pruning are a key goal with document management: we need to keep this short and general, high level lessons not specific frequently-changed facts.

  

# Worktrees & Promotion

  

Fulcrum tasks exclusively use worktree management via the $wt skill. Merge conflict resolution is the responsibility of the implementing Executor agent. As with the implement-plan skill, the responsibility for getting a change through CI and fixing in-scope CI failures lies with the Executor as well.

  

# The Night Watchman

  

Fulcrum operates a specialist scheduled agent called the Night Watchman, Every hour, this agent receives a scheduled Codex message to perform his patrol action. He verifies that all running Codex threads are in the state they’re supposed to be in according to the metadata in ~/brain. Since agents in Fulcrum never “go quiet” without reaching a terminal state, this is mechanically verifiable. If problems are found, the Night Watchman messages the Archon with a report.

  

The Night Watchman is also used for any kind of recurring scheduled agent, as described next.

  

# The Sage

  

The Sage is a scheduled agent who conducts postmortems on project execution. She conducts interviews with agents who have completed their work to identify workflow problems and looks at available log sources. The primary thing we’re looking for here are workflow improvements: what tools are taking too long? What process is burning too many tokens? How could agents be working more efficiently? How could skills be better? How could Fulcrum itself be more efficient? How can we scale Tollgate or our CI process to get work done faster?

  

Postmortems must have actionable suggestions for improvement. After completion, the Sage should file beads with tasks to resolve those issues. The Archon should schedule this cleanup work when appropriate, at its discretion.

  

The default cadence is that the Night Watman wakes the Sage for this process once every 24 hours.

  

# The Inquisitor

  

The Inquisitor is a scheduled agent who performs a thermo-nuclear codebase review (https://github.com/cursor/plugins/blob/main/cursor-team-kit/skills/thermo-nuclear-code-quality-review/SKILL.md). This means looking for the biggest architectural problems and getting them fixed – especially huge files with dozens of different responsibilities, and places where the type system could be used more effectively to make invalid states unrepresentable. Agents focused on individual tasks have difficulty “stepping back” to think about the system as a whole, this is what the Inquisitor does. 

  

As with the Sage, the Inquistor files beads with these architecture improvements and code refactoring jobs, to be scheduled by the Archon.

  

The default cadence is that the Night Wachtman wakes the Sage for this process once every 24 hours, 12 hours offset from the Sage.

  

# Thread Management

  

Threads in Fulcrum come in two forms: user-created, and fulcrum-created.

  

* The Archon and Night Watchman are persistent user-created threads, and they exist perpetually.

* The human user of Fulcrum creates Weaver threads to create tasks, using the $weaver skill, which archive themselves on completion

* The Archon creates Overseers, Executors, Sages, and Inquisitors for plan implementation or on a scheduled basis. For these roles a new thread is used each time and then archived on completion, ensuring work is executed with a fresh perspective.

  

Because Codex currently does not expose a simple way to get the threadId of a newly-created thread, threads must be uniquely identifiable by name. They use tags like [sage-3] and [inquisitor-2] at the start of the thread name so the Archon can document their threadIds in ~/brain and facilitate communication. Paired overseers and executors always use the same numbers so that [overseer-3] maps to [executor-3]. Weavers set their thread name at startup to [weaver-12] etc. 

  

# Model Selection

  

The Archon, Night Watchman, and Weavers are user created and employ whichever model was selected. Overseers, Sages, and Inquisitors use GPT Sol on “High” reasoning. Executors use GPT Luna on “Extra High” reasoning.

  

Models can always be overwritten as part of a prompt or bead, for example “Use Astra High for this, it’s pretty tricky”.

  

# The Fulcrum Dashboard

  

Fulcrum offers a simple persistent localhost dashboard, written in React and operated by the Archon. This dashboard may contain a fork of the https://github.com/mantoni/beads-ui project for visualizing the state of project beads, or directly implement those concepts. The primary entry points to the dashboard are the “status”, “projects”, and “newsfeed” views, exposed in the left side navigation menu on desktop. The default layout of these screens is a two column display showing information cards.

  

- The status view shows an agentic view of the system, which agents are active/scheduled/recently completed

- The projects view shows the current state of projects with a high level summary of their current state and future plans derived from NEWS.md. 

- The newsfeed view shows a task-based view of the system, showing cards over two columns which document current bead status, which beads are currently in-flight, completed, planned, etc.

  

Each project also exposes a project-specific newsfeed, which scopes the displayed beads down to that project.

  

The dashboard should have its own coherent design system which is well documented and designed in advance with purpose. This should feature good use of whitespace, readable font sizes, proper information hierarchy, a beautiful color palette with good contrast, light/dark mode support which follows the OS behavior, and mobile support. Animation is a core part of the Fulcrum Design System. Everything in the system uses material design style transitions, and there are persistent animations for things like “this task is running” or “this agent is in progress”. The dashboard is intended to have kind of a futuristic/video game aesthetic, not look like yet another generic React dashboard.

  

For V1 the dashboard will be read-only, but it’s possible to imagine future versions with simple interactive controls.

  

If we can figure out how to make this possible, I would also like to expose a persistent secure web endpoint for interacting with the dashboard, perhaps via Cloudflare?

  

# Fulcrum Code Style & Linting

  

Fulcrum Python code should use the ‘black’ formatter and ‘pyre’ typechecking library. React code should use typescript, prettier, and eslint. Fulcrum should have its own registered CI system via tollgate. Code should contain only minimal unit tests.

  

# Inspiration

  

Some sources of inspiration for fulcrum include Wheelhouse and Gastown ([https://yegge.ai/listings/the-shape-of-things-to-come](https://yegge.ai/listings/the-shape-of-things-to-come), [https://yegge.ai/listings/fences-not-sandboxes](https://yegge.ai/listings/fences-not-sandboxes), https://yegge.ai/listings/welcome-to-gas-town)**
# Provenance and originality

## What this submission adds

The inspection-focused controller, transactional booking safeguards, least-privilege policy MCP wrapper, authenticated approval service, evaluation harness, security experiment, presentation assets and packaging workflow are developed for this project. The project is deliberately narrower than an all-services municipal chatbot: its write authority is inspection booking, not permit decisions, refunds or outgoing email.

The implementation is AI-assisted. The project owner should review the design and evidence and be prepared to explain the controls, observed failures and limitations during the presentation.

## Course foundation

The synthetic Madar domain and low-level teaching utilities originate from SDAIA Academy's Agentic AI course, revision `e950ba2f14b47deb6c30a586a6d4d5b77944e37d`.

Retained course components include:

- `labs/agentkit.py`: model adapters, tool schemas, agent-loop and MCP helpers.
- `labs/madar.py`: the synthetic domain schema, rules and data access primitives.
- `labs/prepare_data.py`: deterministic synthetic fixture generation.
- `labs/data/policies/`: municipal policy documents.
- `labs/data/AGENTS.md`: the course-provided context, used without editing it.
- `COURSE_README.md` and `labs/capstone/README.md`: course and acceptance references.

These components are credited rather than represented as original inventions. The new application composes and constrains them, rather than presenting the original course starter unchanged.

Course sources:
- https://sdaiaacademy.github.io/agentic-ai/
- https://github.com/SDAIAAcademy/agentic-ai

## Peer examples

The three repositories supplied by the user were reviewed through their README text and file inventories to understand submission packaging. Their implementation source files, secrets, evaluation outputs and graphics were not copied into this project. Their scores are not evidence for this submission.

References reviewed:
- https://github.com/jjujjo/Rafeeq-in-production
- https://github.com/ZiyadAlharthi/rafeeq-municipal-agent
- https://github.com/RahafAlmalki3/Rafeeq-agentic-ai

## Evidence policy

Reported measurements must come from this project's actual test/evaluation artifacts. Model errors and infrastructure failures remain visible. Cached genuine model responses may be used for the explicitly disclosed paired synthetic A/B output-rendering comparison; they are not independent fresh samples and do not establish repeated-trial reliability. Test doubles used by offline protocol tests are not presented as provider responses.

No real municipality is connected. Provider keys, service credentials, raw response caches, local runtime databases and unrelated course materials are excluded from the submission export.

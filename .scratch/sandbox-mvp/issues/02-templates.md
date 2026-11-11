# 02: Templates — `templates`, `save --to`, and `up --from`

**What to build:** Reusable plan templates stored in the templates root, sharing the plan schema. A developer can list templates, save a set's plan as a template, build a template from repos in the current directory, and provision a new set from a template in one command (stories 17–21, 26, 28, 96).

**Blocked by:** 01 (walking skeleton)

**Status:** done

- [x] `sandbox templates` lists available templates
- [x] `sandbox SET save --to TEMPLATE` writes the set's plan as a template
- [x] `sandbox save --to TEMPLATE --repos NAME...` builds a template from repos in the current directory, with absolute `source` paths
- [x] `base` is optional in a template; when absent, `up --from` pins the repo's current default/checked-out branch into the set plan so the set is reproducible
- [x] `sandbox SET up --from TEMPLATE` copies the template into the new set's plan and provisions it using the pinned absolute paths, independent of the current directory
- [x] A template repo whose source lacks `.sandbox/` fails before anything is created
- [x] `--from` and `--repos` are mutually exclusive; there is no template inheritance

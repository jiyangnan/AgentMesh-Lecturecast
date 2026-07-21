# LectureCast system boundary — Public client and site view

Canonical decision: [lecturecast system boundary](https://github.com/jiyangnan/lecturecast/blob/main/docs/LECTURECAST-SYSTEM-BOUNDARY.md).

## This repository owns

- The public commercial client, agent adapters, local renderer, component
  allowlist, signed-manifest verification, local project state, and offline
  rerender/recovery.
- The canonical public website source under `site/`.

## Production website rule

- `lecturecast.agentmesh360.com` is served by the existing AgentMesh360
  `jobagent-caddy` from `/srv/web/lecturecast`.
- `agentmesh-core` owns the shared Caddy configuration, production SSH Secret,
  exact-commit static-site deployment workflow, verification, and rollback.
- This repository must not contain a GitHub Pages production workflow, a
  production `site/CNAME`, a production SSH Secret, or a second ingress.
- GitHub Pages is not a production origin for LectureCast.
- `.github/workflows/site-contract.yml` remains a read-only CI gate. Passing CI
  does not mean the site is deployed.

## Product boundary

LectureCast has one commercial product path. The client must validate a universal
AgentMesh360 API Key, active paid access and sufficient shared credits
before production begins. Director returns choices, a Creative Brief and a
signed declarative `ProductionManifest`; original media, voice, subtitles,
editing, rendering, covers and exports stay on the user's machine. All commercial
checks fail closed, and Manifest generation remains metered through shared
AgentMesh360 credits.

## Reverse links

- [Canonical product boundary](https://github.com/jiyangnan/lecturecast/blob/main/docs/LECTURECAST-SYSTEM-BOUNDARY.md)
- [Private Director boundary](https://github.com/jiyangnan/lecturecast-server/blob/main/docs/LECTURECAST-SYSTEM-BOUNDARY.md)
- [Core gateway boundary](https://github.com/jiyangnan/agentmesh-core/blob/main/docs/LECTURECAST-SYSTEM-BOUNDARY.md)
- [Deployment operator boundary](https://github.com/jiyangnan/agentmesh-deploy/blob/main/docs/LECTURECAST-SYSTEM-BOUNDARY.md)

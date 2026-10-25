# avdlab frontend

Vite + React 18 + TypeScript + Tailwind + TanStack Query + react-router + @xyflow/react.

```
npm install
npm run dev              # against backend on http://localhost:8000 (proxied /api + websocket)
VITE_MOCK=1 npm run dev  # in-browser MSW mock of the whole API (login: any user/password)
npm run build            # -> dist/ (served by the backend / Dockerfile.backend)
npm run lint
npm test                 # vitest
```

## Layout

- `src/api/*` — typed fetch client (`credentials: 'include'`, 401 → /login), one module per backend router; types mirror `backend/avdlab/**/api.py`.
- `src/pages/*` — Login, Dashboard, Hosts, Devcontainer, Images, Labs, NewLab (wizard), Jobs, Job; `src/pages/lab/*` — lab detail tabs
  (Overview, Settings, Files, Build, Topology, Jobs).
- `src/components/JobDrawer.tsx` + `src/hooks/useJobStream.ts` — any 202 `{job_id}` goes through `drawer.track()`; streams
  `WS /api/jobs/{id}/stream` (`{line}` / `{done}`) and polls `GET /api/jobs/{id}` for the status badge.
- `src/components/KnobForm.tsx` — form generated from template `knobs[]` (int/string/cidr/ipv4/secret/object/yaml/bool); 422 violations mapped inline.
- `src/lib/topology.ts` — `/api/topology/{lab_id}` JSON → React Flow nodes/edges layered by role.
- `src/mocks/*` — MSW handlers for every endpoint + the job WebSocket (2 lab hosts, one built lab with 2 spines / 4 leaves / 2 servers).
  Mock-only behaviour: catalogue returns 409 until a secret named `arista_token` exists; `leaf_pairs > 4` or `spine_count > 4` → 422.

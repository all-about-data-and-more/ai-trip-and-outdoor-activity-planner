# Screenshots

Drop three PNGs here with these exact names — the top-level README references them:

| File | What to capture |
|---|---|
| `app.png` | The main trip planner UI — a trip selected, with a destination and its activities visible. |
| `itinerary.png` | The generated **itinerary + packing list** (ideally showing the weather strip and a "rescheduled" badge, or the explain panel). |
| `agent.png` | The **Agent Bricks agent** in the AI Playground calling an MCP tool (the `Tools (1)` view with a tool result), **or** the deployed agent's chat answering a planning request. |

Tips for good screenshots:
- Use a real trip with real attractions (run `scripts/enrich_destinations.py` first so destinations
  have activities).
- Crop to the relevant panel; keep them readable at ~1200px wide.
- PNG, ideally under ~500 KB each.

Once added:
```bash
git add docs/screenshots/*.png
git commit -m "docs: add app screenshots"
git push
```

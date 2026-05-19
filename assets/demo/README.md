# Demo media

The root `README.md` references the screenshots below. Keep filenames stable
or update the README in lockstep.

## Expected files

| Filename | What to capture | Aspect / size guidance |
|----------|-----------------|------------------------|
| `dashboard.png` | The macOS dashboard at rest, FLOW state, with real biometrics visible (HR, HRV, blink rate). Use the Dashboard tab unless the Advanced tab is the feature being documented. | 420 × 700 minimum; raw app capture preferred |
| `overlay.png` | The real intervention overlay injected into a safe demo Chrome tab, showing headline + causal explanation + per-tab recommendations + single CTA | 1280 × 720 or larger |
| `pulse-room.png` | The real Pulse Room new-tab page with the central orb pulsing at a visible HR | 1280 × 720 or larger |

Optional extras (drop in if you have them):

- `hero.gif` — 8–15 s loop of a full intervention cycle: HYPER detected → overlay appears → causal explanation visible → one-click execute → tabs close → undo button reachable. Target 1280 × ~720, < 6 MB, 12–18 fps.
- `architecture.svg` — exported from a diagramming tool (Excalidraw / Mermaid live editor / Figma) showing the L1→L5 layered pipeline. Replaces the ASCII block in the README "How It Works" section.
- `state-classification.png` — a still from a session showing the state transition timeline.

## Capture tips

- Use a safe local demo workspace for the browser-overlay screenshot so no private tab titles or file paths leak.
- Capture the dashboard and Pulse Room in the appearance currently implemented by the product; do not recolor screenshots to imply a theme the page does not render.
- For the GIF, [Gifox](https://gifox.io/) or `ffmpeg` produces smaller files than Kap.
- Optimise with [gifsicle](https://www.lcdf.org/gifsicle/) — `gifsicle -O3 --lossy=80` typically halves file size with no visible quality loss.

## Privacy

Never include identifiable workspace context — file paths from real
projects, real tab titles with sensitive content, real terminal output
containing secrets. Use a dedicated demo profile.

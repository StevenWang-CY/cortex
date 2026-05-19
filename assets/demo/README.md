# Demo media

Drop screenshots and the hero GIF here. The root `README.md` references
the filenames below — keep them stable or update the README in lockstep.

## Expected files

| Filename | What to capture | Aspect / size guidance |
|----------|-----------------|------------------------|
| `hero.gif` | 8–15 s loop of a full intervention cycle: HYPER detected → overlay appears → causal explanation visible → one-click execute → tabs close → undo button reachable | 1280 × ~720, < 6 MB, 12–18 fps |
| `dashboard.png` | The macOS dashboard at rest, FLOW state, with real biometrics visible (HR, HRV, blink rate). Both tabs (Biometrics + Advanced) acceptable; Biometrics is the more polished surface. | 1600 × 1000, dark mode preferred |
| `overlay.png` | The intervention overlay live on a Chrome tab, showing headline + causal explanation + per-tab recommendations + single CTA | 1600 × 1000, real (not staged) content |
| `pulse-room.png` | The Pulse Room new-tab page with the central orb pulsing at a visible HR | 1600 × 1000 |

Optional extras (drop in if you have them):

- `architecture.svg` — exported from a diagramming tool (Excalidraw / Mermaid live editor / Figma) showing the L1→L5 layered pipeline. Replaces the ASCII block in the README "How It Works" section.
- `state-classification.png` — a still from a session showing the state transition timeline.

## Capture tips

- Use macOS Light Mode for the browser-overlay screenshot (better contrast on white pages).
- Use macOS Dark Mode for the dashboard / Pulse Room shots (Cortex is designed dark).
- For the GIF, [Gifox](https://gifox.io/) or `ffmpeg` produces smaller files than Kap.
- Optimise with [gifsicle](https://www.lcdf.org/gifsicle/) — `gifsicle -O3 --lossy=80` typically halves file size with no visible quality loss.

## Privacy

Never include identifiable workspace context — file paths from real
projects, real tab titles with sensitive content, real terminal output
containing secrets. Use a dedicated demo profile.

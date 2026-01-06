# EasyADSB Screenshot Guide

Screenshots for README.md documentation (v1.3.0)

---

## Automated Screenshot Generator

Screenshots are generated automatically using Puppeteer. The tool is located at:
```
/home/mortyai/projects/easyadsb-screenshots/
```

### Quick Usage

```bash
cd /home/mortyai/projects/easyadsb-screenshots

# Generate all screenshots
npm run all

# Generate only terminal (CLI) screenshots
npm run terminal

# Generate only dashboard screenshots
npm run dashboard
```

Screenshots are saved to `./output/`

---

## Terminal Screenshots (Styled HTML)

These are generated from HTML templates - no actual commands are run. Edit `generate.js` to modify content.

| File | Description |
|------|-------------|
| `setup-start.png` | Initial setup screen with prerequisites check |
| `setup-menu.png` | Main management menu (8 options) |
| `setup-location.png` | Location configuration step |
| `setup-complete.png` | Setup complete with dashboard URLs |
| `docker-status.png` | Docker compose ps output |
| `git-clone.png` | Clone and run commands |

### Editing Terminal Content

In `generate.js`, find the `TERMINAL_SCREENS` array:

```javascript
const TERMINAL_SCREENS = [
  {
    name: 'setup-start',
    title: 'EasyADSB Setup',
    content: `
      <div class="line"><span class="prompt">$</span> <span class="command">./setup.sh</span></div>
      ...
    `
  },
  // Add more screens here
];
```

Available CSS classes for styling:
- `.prompt` - Green prompt ($)
- `.command` - White command text
- `.comment` - Gray comments (#)
- `.success` - Green success text
- `.warning` - Yellow warning text
- `.error` - Red error text
- `.info` - Blue info text
- `.dim` - Muted gray text

---

## Dashboard Screenshots (Live Captures)

These are real screenshots captured from your running dashboard.

| File | Selector | Description |
|------|----------|-------------|
| `dashboard-full-dashboard.png` | Full page | Complete dashboard view |
| `dashboard-header.png` | `.header` | Header with title and controls |
| `dashboard-feed-status.png` | `#feeders-section` | All 6 feed status cards |
| `dashboard-live-map.png` | `#live-map` | Interactive aircraft map |
| `dashboard-quick-stats.png` | `#quick-stats-section` | Aircraft count, messages, range |
| `dashboard-live-aircraft.png` | `#live-aircraft` | Recent aircraft table |
| `dashboard-station-ids.png` | `#station-ids-section` | Station credentials |
| `dashboard-leaderboard.png` | `#leaderboard-section` | Aircraft leaderboard |
| `dashboard-spotter-stats.png` | `#stats-section` | Statistics and records |

### Adding More Dashboard Sections

In `generate.js`, find the `DASHBOARD_SCREENS` array:

```javascript
const DASHBOARD_SCREENS = [
  { name: 'full-dashboard', selector: null, fullPage: true },
  { name: 'live-map', selector: '#live-map' },
  // Add more sections:
  { name: 'achievements', selector: '#achievements-section' },
];
```

### Configuration

Edit `CONFIG` in `generate.js`:

```javascript
const CONFIG = {
  dashboardUrl: 'http://192.168.2.60:8081',  // Your Pi's IP
  outputDir: './output',
  viewport: { width: 1280, height: 800 },
  terminalViewport: { width: 900, height: 600 }
};
```

---

## Copying to Repository

After generating screenshots:

```bash
# Copy to main repo screenshots folder
cp /home/mortyai/projects/easyadsb-screenshots/output/*.png /home/mortyai/easyadsb-main/screenshots/

# Or copy specific files
cp output/dashboard-full-dashboard.png ~/easyadsb-main/screenshots/dashboard-full.png
```

---

## Manual Screenshots (If Needed)

### Terminal Screenshots

For any CLI screenshots not covered by the generator:

1. Make terminal window wide (120+ chars)
2. Run the command
3. Use a screenshot tool or `gnome-screenshot -a`

### Dashboard Screenshots

1. Open `http://YOUR-PI-IP:8081`
2. Enable dark mode (click moon icon)
3. Enable stealth mode for Station IDs section (hides real keys)
4. Use browser dev tools to capture specific sections

---

## Screenshot Checklist for v1.3.0

### Required for README

- [ ] `dashboard-full.png` - Hero shot of full dashboard
- [ ] `setup-menu.png` - CLI management menu

### Optional (Nice to Have)

- [ ] `dashboard-map.png` - Just the interactive map
- [ ] `dashboard-achievements.png` - Achievements section
- [ ] `dashboard-leaderboard.png` - Leaderboard section
- [ ] `dashboard-gallery.png` - Aircraft type gallery
- [ ] `setup-complete.png` - Setup success screen
- [ ] `docker-status.png` - Container status

---

## Tips

- **Wait for data**: Let services run 10-15 minutes before dashboard screenshots
- **Stealth mode**: Always enable when showing Station IDs
- **Dark mode**: Use dark mode for consistency
- **Real aircraft**: Screenshots look better with actual aircraft on the map
- **Consistent sizing**: The generator handles this automatically

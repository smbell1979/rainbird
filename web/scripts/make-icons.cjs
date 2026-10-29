// Render src/app/apple-icon.png from src/app/icon.svg. Run after changing the SVG:
//   node scripts/make-icons.cjs
// The home-screen icon sits on the dark Dracula background like CG-Stuff's,
// because iOS fills transparency with black and the dark stroke would vanish.
/* eslint-disable @typescript-eslint/no-require-imports -- a CommonJS script run directly by node, not app code */
const path = require("path");
const sharp = require("sharp");

const app = path.join(__dirname, "..", "src", "app");
const SIZE = 180;
const ICON = 124; // the cube fills about this much of CG-Stuff's 180 px tile

(async () => {
  const icon = await sharp(path.join(app, "icon.svg"), { density: 600 }).resize(ICON, ICON).png().toBuffer();
  const pad = (SIZE - ICON) / 2;
  await sharp({ create: { width: SIZE, height: SIZE, channels: 4, background: "#21222c" } })
    .composite([{ input: icon, left: pad, top: pad }])
    .png()
    .toFile(path.join(app, "apple-icon.png"));
  console.log("wrote", path.join(app, "apple-icon.png"));
})();

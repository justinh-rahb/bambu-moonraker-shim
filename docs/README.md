# Shambu project page

A dependency-free landing page for Bambu Moonraker Shim. The page uses relative asset links so it works under the repository's GitHub Pages subpath, and remains usable without JavaScript. JavaScript only adds clipboard copying.

## Preview

From the repository root:

```sh
python3 -m http.server 8765 --bind 127.0.0.1 --directory docs
```

Open <http://127.0.0.1:8765/>.

## Publish on GitHub Pages

In repository **Settings → Pages → Build and deployment**, select **GitHub Actions** as the source. The `Deploy project page` workflow publishes `docs/` after relevant changes reach `master`. It can also be run manually from Actions.

With the existing repository name, the public URL is `https://justinh-rahb.github.io/bambu-moonraker-shim/`. Shambu is the display name; the Python package, image name and repository path remain unchanged.

## Assets and content

- `assets/shambu.svg`: local vector mark, an S with opposing translation arrows.
- `assets/dashboard.jpg`: read-only Chrome capture of the local P1S dashboard on November 1, 2026. The instance still displays Fakeraker. The landing page shows a cropped preview with a link to the complete image.
- `screenshot.png`: original README screenshot, retained for existing links.
- `index.html`, `style.css`, `script.js`: page source.

Keep capability and validation claims aligned with the main README. Screenshots are illustrative and do not establish support for every visible control or model.

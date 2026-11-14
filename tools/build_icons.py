"""Build the application's small, flat signal mark at every packaging size.

The SVG and raster outputs use the same geometry. Requires Pillow from
requirements-build.txt; no network assets or platform-specific converters.
"""

from pathlib import Path
import math

from PIL import Image, ImageDraw


def main():
    destination = Path(__file__).resolve().parents[1] / "assets"
    destination.mkdir(exist_ok=True)
    scale = 4
    image = Image.new("RGBA", (1024 * scale, 1024 * scale))
    draw = ImageDraw.Draw(image)
    background, foreground = "#233649", "#e8f0f7"
    draw.rounded_rectangle(tuple(value * scale for value in (88, 88, 936, 936)),
                           radius=190 * scale, fill=background)
    paths = []
    for radius in (350, 235, 120):
        # Pillow places the arc stroke inside its bounding box; SVG centers it.
        outer = radius + 23
        draw.arc(tuple(value * scale for value in (
            512 - outer, 735 - outer, 512 + outer, 735 + outer)),
            start=220, end=320, fill=foreground, width=46 * scale)
        ends = []
        for angle in (220, 320):
            x = 512 + radius * math.cos(math.radians(angle))
            y = 735 + radius * math.sin(math.radians(angle))
            ends.append((x, y))
            draw.ellipse(tuple(value * scale for value in (x - 23, y - 23, x + 23, y + 23)),
                         fill=foreground)
        paths.append(f'M {ends[0][0]:.2f} {ends[0][1]:.2f} '
                     f'A {radius} {radius} 0 0 1 {ends[1][0]:.2f} {ends[1][1]:.2f}')
    draw.ellipse(tuple(value * scale for value in (484, 707, 540, 763)), fill=foreground)
    master = image.resize((1024, 1024), Image.Resampling.LANCZOS)
    master.resize((256, 256), Image.Resampling.LANCZOS).save(destination / "wifi-analyzer.png")
    master.save(destination / "wifi-analyzer.icns", format="ICNS")
    master.save(destination / "wifi-analyzer.ico", format="ICO",
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    svg_paths = "\n  ".join(f'<path d="{path}"/>' for path in paths)
    (destination / "wifi-analyzer.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">\n'
        '  <title>WiFi Analyzer</title>\n'
        f'  <rect x="88" y="88" width="848" height="848" rx="190" fill="{background}"/>\n'
        f'  <g fill="none" stroke="{foreground}" stroke-width="46" stroke-linecap="round">\n'
        f'  {svg_paths}\n  </g>\n'
        f'  <circle cx="512" cy="735" r="28" fill="{foreground}"/>\n</svg>\n'
    )
    print(f"Built application icons in {destination}")


if __name__ == "__main__":
    main()

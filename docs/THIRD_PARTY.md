# Bundled terminal dependencies

Browser terminal assets are vendored so local/offline startup requires no npm or CDN.

| Component | Version | Source | License file |
|---|---|---|---|
| @xterm/xterm | 6.0.0 | https://www.npmjs.com/package/@xterm/xterm | dist/vendor/xterm-LICENSE |
| @xterm/addon-fit | 0.11.0 | https://www.npmjs.com/package/@xterm/addon-fit | dist/vendor/addon-fit-LICENSE |

Unmodified browser JS/CSS and license texts were extracted from the official npm tarballs. Browser developer tools may request optional sourcemaps; they are not shipped and are not needed at runtime.

On Windows, requirements.txt pins pywinpty 3.0.5 (MIT), installed from PyPI. No Python terminal dependency is required on POSIX.

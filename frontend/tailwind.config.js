/** Colours come from CSS variables (see src/index.css) so the whole UI switches between light and dark. */
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;

/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["class"],
  theme: {
    extend: {
      colors: {
        ink: { 950: v("ink-950"), 900: v("ink-900"), 850: v("ink-850"), 800: v("ink-800"), 700: v("ink-700"), 600: v("ink-600") },
        slate: { 100: v("t-100"), 200: v("t-200"), 300: v("t-300"), 400: v("t-400"), 500: v("t-500"), 600: v("t-600") },
        fg: v("fg"),
        accent: { DEFAULT: v("accent"), soft: v("accent-soft"), deep: v("accent-deep") },
        ios: { green: v("green"), orange: v("orange"), red: v("red"), purple: v("purple"), teal: v("teal"), pink: v("pink") },
      },
      fontFamily: {
        // Apple's system fonts on macOS/iOS, Segoe UI Variable (Windows 11's equivalent) elsewhere. Nothing is downloaded.
        sans: ["-apple-system", "BlinkMacSystemFont", "SF Pro Text", "SF Pro Display", "Segoe UI Variable Text", "Segoe UI Variable Display", "Segoe UI", "system-ui", "sans-serif"],
        display: ["-apple-system", "BlinkMacSystemFont", "SF Pro Display", "Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI", "system-ui", "sans-serif"],
        mono: ["SF Mono", "ui-monospace", "Cascadia Code", "Cascadia Mono", "Consolas", "monospace"],
      },
      borderRadius: { "2xl": "1.125rem", "3xl": "1.5rem", "4xl": "2rem" },
      boxShadow: {
        card: "0 1px 2px rgb(0 0 0 / 0.06), 0 8px 24px -8px rgb(0 0 0 / 0.18)",
        float: "0 10px 40px -10px rgb(0 0 0 / 0.45), 0 2px 6px rgb(0 0 0 / 0.2)",
      },
      keyframes: {
        pop: { from: { opacity: "0", transform: "scale(0.97) translateY(6px)" }, to: { opacity: "1", transform: "none" } },
        fade: { from: { opacity: "0" }, to: { opacity: "1" } },
      },
      animation: { pop: "pop 0.22s cubic-bezier(0.2, 0.8, 0.2, 1)", fade: "fade 0.18s ease-out" },
    },
  },
  plugins: [],
};

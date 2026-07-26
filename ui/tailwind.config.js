/** @type {import('tailwindcss').Config} */
export default {
  // Theme is toggled by a `.light` / `.dark` class on <html> (see src/theme.ts).
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Tier colour code — constant across themes so the pie/bars read
        // instantly and the brand mark stays tied to the product palette.
        cheap: "#22c55e",     // green
        mid: "#3b82f6",       // blue
        frontier: "#ef4444",  // red
        // Structural + neutral palette resolves through CSS variables so a
        // single class swap re-themes every panel. Channels are space-separated
        // RGB (see src/index.css) so Tailwind's /opacity modifiers still work.
        ink: "rgb(var(--c-ink) / <alpha-value>)",       // page background
        panel: "rgb(var(--c-panel) / <alpha-value>)",   // card surface
        edge: "rgb(var(--c-edge) / <alpha-value>)",     // borders
        accent: "rgb(var(--c-accent) / <alpha-value>)", // selection / focus
        gray: {
          100: "rgb(var(--c-gray-100) / <alpha-value>)",
          200: "rgb(var(--c-gray-200) / <alpha-value>)",
          300: "rgb(var(--c-gray-300) / <alpha-value>)",
          400: "rgb(var(--c-gray-400) / <alpha-value>)",
          500: "rgb(var(--c-gray-500) / <alpha-value>)",
          600: "rgb(var(--c-gray-600) / <alpha-value>)",
          700: "rgb(var(--c-gray-700) / <alpha-value>)",
        },
      },
    },
  },
  plugins: [],
};

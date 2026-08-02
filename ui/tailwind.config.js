/** @type {import('tailwindcss').Config} */
export default {
  // Theme is toggled by a `.light` / `.dark` class on <html> (see src/theme.ts).
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Semantic signal palette (mirror of ui/src/tokens.ts). Constant across
        // themes so charts/bars read instantly.
        cheap: "#35D6A0",
        mid: "#4C8DFF",
        frontier: "#F5A524",
        privacy: "#8B7BF7",
        alert: "#F2555A",
        // Structural neutrals resolve through CSS variables (src/index.css).
        ink: "rgb(var(--c-ink) / <alpha-value>)",
        panel: "rgb(var(--c-panel) / <alpha-value>)",
        edge: "rgb(var(--c-edge) / <alpha-value>)",
        accent: "rgb(var(--c-accent) / <alpha-value>)",
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
      fontFamily: {
        display: ['Archivo', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        sans: ['"IBM Plex Sans"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'SFMono-Regular', 'monospace'],
      },
      keyframes: {
        "rail-pulse": {
          "0%": { opacity: "0.15" },
          "50%": { opacity: "1" },
          "100%": { opacity: "0.15" },
        },
      },
      animation: {
        "rail-pulse": "rail-pulse 1.2s ease-in-out",
      },
    },
  },
  plugins: [],
};

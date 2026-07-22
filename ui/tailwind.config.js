/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Tier colour code — reused everywhere so the pie/bars read instantly.
        cheap: "#22c55e",     // green
        mid: "#3b82f6",       // blue
        frontier: "#ef4444",  // red
        ink: "#0b0f19",       // page background (dark, projector-legible)
        panel: "#111827",
        edge: "#1f2937",
      },
    },
  },
  plugins: [],
};

import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        surface: {
          950: "#09090b",
          900: "#111113",
          800: "#1c1c1f",
        },
        accent: {
          400: "#38bdf8",
          500: "#0ea5e9",
          600: "#0284c7",
        },
      },
      boxShadow: {
        panel: "0 18px 50px rgb(0 0 0 / 0.28)",
      },
    },
  },
  plugins: [],
} satisfies Config;

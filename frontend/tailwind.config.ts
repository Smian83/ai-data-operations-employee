import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        brand: {
          background: "#050816",
          purple: "#8b5cf6",
          bright: "#a855f7",
          blue: "#3b82f6",
        },
      },
      boxShadow: {
        glow: "0 0 34px rgba(139, 92, 246, 0.18)",
      },
    },
  },
  plugins: [],
};

export default config;

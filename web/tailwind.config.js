/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        dungeon: {
          bg: "#12100e",
          panel: "#1c1917",
          edge: "#3f3a33",
          gold: "#c9a959",
          blood: "#8c2f2f",
          ink: "#d6cdbb",
        },
      },
      fontFamily: {
        display: ['"Cinzel"', "serif"],
        body: ['"Alegreya"', "serif"],
      },
    },
  },
  plugins: [],
};

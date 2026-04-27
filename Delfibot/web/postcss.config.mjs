// Tailwind v4 ships its PostCSS plugin as `@tailwindcss/postcss`. The
// older v3 syntax (require("tailwindcss"), require("autoprefixer")) is
// rejected by the v4 build pipeline.
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;

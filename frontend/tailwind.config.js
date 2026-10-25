/** @type {import('tailwindcss').Config} */
const cv = (name) => `rgb(var(--cv-${name}) / <alpha-value>)`

export default {
  darkMode: 'media',
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      // CONTRACT-8 B design tokens (defined as CSS variables in src/index.css)
      colors: {
        cv: {
          nav: cv('nav'),
          'nav-hover': cv('nav-hover'),
          'nav-active': cv('nav-active'),
          'nav-border': cv('nav-border'),
          'nav-text': cv('nav-text'),
          'nav-text-active': cv('nav-text-active'),
          'nav-muted': cv('nav-muted'),
          accent: cv('accent'),
          primary: cv('primary'),
          'primary-hover': cv('primary-hover'),
          'primary-soft': cv('primary-soft'),
          'primary-text': cv('primary-text'),
          bg: cv('bg'),
          surface: cv('surface'),
          'surface-2': cv('surface-2'),
          topbar: cv('topbar'),
          border: cv('border'),
          'border-strong': cv('border-strong'),
          text: cv('text'),
          muted: cv('muted'),
          'hint-bg': cv('hint-bg'),
          'hint-border': cv('hint-border'),
          'hint-text': cv('hint-text'),
        },
      },
    },
  },
  plugins: [],
}

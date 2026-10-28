export default {
  title: 'Документация проекта',
  tagline: 'Тестовая документация',
  url: process.env.DOCS_SITE_URL || 'https://example.vercel.app',
  baseUrl: '/',
  trailingSlash: true,
  onBrokenLinks: 'throw',
  i18n: {defaultLocale: 'ru', locales: ['ru']},
  markdown: {mermaid: true},
  themes: ['@docusaurus/theme-mermaid'],
  presets: [['classic', {
    docs: {path: 'content', routeBasePath: '/', sidebarPath: './sidebars.js'},
    blog: false,
    pages: false,
    sitemap: false,
  }]],
  themeConfig: {
    navbar: {title: 'Документация'},
    mermaid: {theme: {light: 'neutral', dark: 'dark'}},
  },
};

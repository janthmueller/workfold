import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://janthmueller.github.io',
  base: '/wuf',
  scopedStyleStrategy: 'where',
  integrations: [
    starlight({
      title: 'Wuf',
      description: 'Documentation for the Wuf Python CLI.',
      disable404Route: true,
      customCss: ['./src/styles/custom.css'],
      social: [
        {
          icon: 'github',
          label: 'GitHub',
          href: 'https://github.com/janthmueller/wuf',
        },
      ],
      sidebar: [
        {
          label: 'Start Here',
          items: [
            { label: 'Overview', link: '/' },
            { label: 'Using Wuf', link: '/guides/usage/' },
            { label: 'Development', link: '/guides/development/' },
            { label: 'Architecture', link: '/reference/architecture/' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'Coverage and accuracy', link: '/reference/accuracy/' },
          ],
        },
      ],
    }),
  ],
});

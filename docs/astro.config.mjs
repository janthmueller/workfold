import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://janthmueller.github.io',
  base: '/wuf',
  scopedStyleStrategy: 'where',
  integrations: [
    starlight({
      title: 'WUF Unifies Footprints',
      description: 'Documentation for the Wuf terminal CLI.',
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
          label: 'Wuf',
          items: [
            { label: 'Overview', link: '/' },
            { label: 'Using Wuf', link: '/guides/usage/' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'Coverage and accuracy', link: '/reference/accuracy/' },
          ],
        },
        {
          label: 'Contributing',
          items: [
            { label: 'Development', link: '/guides/development/' },
          ],
        },
      ],
    }),
  ],
});

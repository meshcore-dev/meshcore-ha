import type {ReactNode} from 'react';
import clsx from 'clsx';
import Heading from '@theme/Heading';
import styles from './styles.module.css';
import Link from '@docusaurus/Link';

type FeatureItem = {
  title: string;
  description: ReactNode;
  icon: string;
};

const FeatureList: FeatureItem[] = [
  {
    title: 'Seamless Integration',
    icon: '🏠',
    description: (
      <>
        Connect your Meshcore devices directly to Home Assistant for unified
        control of your smart home ecosystem.
      </>
    ),
  },
  {
    title: 'Real-time Updates',
    icon: '⚡',
    description: (
      <>
        Get instant status updates and control your devices with minimal latency
        through the Meshcore WebSocket connection.
      </>
    ),
  },
  {
    title: 'Automation Ready',
    icon: '🤖',
    description: (
      <>
        Use Meshcore device states and services in your Home Assistant
        automations for powerful smart home scenarios.
      </>
    ),
  },
];

const ResourceLinks = [
  {
    title: 'Website',
    url: 'https://meshcore.co.uk',
    logo: '/meshcore-ha/img/meshcore-ha-icon.png',
  },
  {
    title: 'GitHub',
    url: 'https://github.com/meshcore-dev/Meshcore',
    logo: '/meshcore-ha/img/github-logo.svg',
  },
  {
    title: 'Discord',
    url: 'https://discord.gg/meshcore',
    logo: '/meshcore-ha/img/discord-logo.svg',
  },
  {
    title: 'Python SDK',
    url: 'https://github.com/meshcore-dev/meshcore_py',
    logo: '/meshcore-ha/img/python-logo.svg',
  },
];

function Feature({title, icon, description}: FeatureItem) {
  return (
    <div className={clsx('col col--4')}>
      <div className="text--center">
        <div className={styles.featureIcon}>{icon}</div>
      </div>
      <div className="text--center padding-horiz--md">
        <Heading as="h3">{title}</Heading>
        <p>{description}</p>
      </div>
    </div>
  );
}

export default function HomepageFeatures(): ReactNode {
  return (
    <>
      <section className={styles.features}>
        <div className="container">
          <div className="row">
            {FeatureList.map((props, idx) => (
              <Feature key={idx} {...props} />
            ))}
          </div>
        </div>
      </section>
      <section className={styles.resources}>
        <div className="container">
          <Heading as="h2" className="text--center margin-bottom--lg">
            Meshcore Resources
          </Heading>
          <div className="row">
            {ResourceLinks.map((link, idx) => (
              <div key={idx} className="col col--3">
                <Link
                  className={clsx('button button--outline button--primary button--lg', styles.resourceButton)}
                  to={link.url}>
                  <img src={link.logo} alt={link.title} 
                    className={link.logo.endsWith('.png') ? styles.resourceLogoPng : styles.resourceLogo} />
                  {link.title}
                </Link>
              </div>
            ))}
          </div>
        </div>
      </section>
    </>
  );
}
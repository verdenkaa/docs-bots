import React from 'react';
import {Redirect, useLocation} from '@docusaurus/router';

export default function Home() {
  const {search, hash} = useLocation();
  return <Redirect to={{pathname: '/architecture/', search, hash}} />;
}

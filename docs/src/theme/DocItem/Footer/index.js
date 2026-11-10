import React from 'react';
import {useDoc} from '@docusaurus/plugin-content-docs/client';
import OriginalFooter from '@theme-original/DocItem/Footer';
import Comments from '../../../components/Comments';

export default function DocFooter(props) {
  const {metadata} = useDoc();
  return (
    <>
      <OriginalFooter {...props} />
      <Comments key={metadata.permalink} page={metadata.permalink} />
    </>
  );
}

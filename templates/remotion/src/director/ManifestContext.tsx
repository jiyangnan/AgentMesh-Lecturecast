import React, {createContext, useContext} from 'react';
import {DirectorRenderProps} from './types';

const ManifestContext = createContext<DirectorRenderProps | null>(null);

export const ManifestProvider: React.FC<React.PropsWithChildren<DirectorRenderProps>> = ({children, ...value}) => (
  <ManifestContext.Provider value={value}>{children}</ManifestContext.Provider>
);

export const useManifest = (): DirectorRenderProps => {
  const value = useContext(ManifestContext);
  if (!value) throw new Error('ManifestProvider is required');
  return value;
};


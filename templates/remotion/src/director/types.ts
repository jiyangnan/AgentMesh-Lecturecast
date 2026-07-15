export type AspectRatio = '16:9' | '9:16' | '3:4';
export type MediaType = 'image' | 'video' | 'audio' | 'document';

export type ManifestAsset = {
  asset_id: string;
  uri: string;
  media_type: MediaType;
  required: boolean;
};

export type ManifestScene = {
  scene_id: string;
  section_id: string;
  component_id: string;
  start_frame: number;
  duration_frames: number;
  props: Record<string, unknown>;
  assets: ManifestAsset[];
  transition_in: {transition_id: 'cut' | 'fade' | 'slide'; duration_frames: number};
};

export type ProductionManifest = {
  schema_version: '1.0';
  manifest_id: string;
  fps: number;
  total_frames: number;
  script: Array<{
    section_id: string;
    title: string;
    narration: string;
    start_frame: number;
    duration_frames: number;
  }>;
  scenes: ManifestScene[];
  outputs: Array<{
    output_id: string;
    kind: 'video' | 'cover';
    aspect_ratio: AspectRatio;
    width: number;
    height: number;
    format: 'mp4' | 'png';
    filename: string;
  }>;
  visual?: {palette?: string[]};
  [key: string]: unknown;
};

export type LocalOverrides = {
  scene_props?: Record<string, Record<string, unknown>>;
  scene_timing?: Record<string, {start_frame?: number; duration_frames?: number}>;
  cover?: {eyebrow?: string; title?: string; subtitle?: string};
};

export type DirectorRenderProps = {
  manifest: ProductionManifest;
  overrides: LocalOverrides;
  variant: 'vertical' | 'landscape';
  audioSrc?: string;
};

export type SceneComponentProps = {
  scene: ManifestScene;
  props: Record<string, unknown>;
  variant: 'vertical' | 'landscape';
};


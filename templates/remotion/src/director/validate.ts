import catalog from './component-catalog.json';
import {ProductionManifest} from './types';

export const validateManifestForRender = (manifest: ProductionManifest): void => {
  if (manifest.schema_version !== '1.0') throw new Error('unsupported manifest schema_version');
  if (!Number.isInteger(manifest.total_frames) || manifest.total_frames <= 0) throw new Error('invalid total_frames');
  const componentIds = new Set(catalog.components.map((component) => component.component_id));
  const sceneIds = new Set<string>();
  for (const scene of manifest.scenes) {
    if (sceneIds.has(scene.scene_id)) throw new Error(`duplicate scene_id ${scene.scene_id}`);
    sceneIds.add(scene.scene_id);
    if (!componentIds.has(scene.component_id)) throw new Error(`unknown component ${scene.component_id}`);
    if (scene.start_frame < 0 || scene.duration_frames <= 0 || scene.start_frame + scene.duration_frames > manifest.total_frames) {
      throw new Error(`scene timing exceeds manifest: ${scene.scene_id}`);
    }
  }
};


import { useLayoutEffect, useRef, useState } from 'react';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import type { Matrix, Point, Pose } from './types';
import { boundedPoints, MAX_POINTS, MAX_POSES, poseVisible, scenePoint, scenePose, scenePosition, trajectoryLines } from './data';

export type MapModel = { points: Point[]; poses: Pose[]; camera: Matrix | null; map: number; segment: number };
type Runtime = { update: (value: MapModel) => void; fit: () => void };

export function MapView({ model }: { model: MapModel }) {
  const host = useRef<HTMLDivElement>(null);
  const runtime = useRef<Runtime | null>(null);
  const [error, setError] = useState('');
  const count = Math.min(model.points.length, MAX_POINTS);
  const edges = trajectoryLines(model.poses, model.map, model.segment).length / 2;

  useLayoutEffect(() => {
    if (!host.current) return;
    let renderer: THREE.WebGLRenderer;
    try { renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false }); }
    catch { setError('WebGL 2 недоступний у цьому браузері. Кадри й числові результати залишаються доступними.'); return; }
    const element = host.current;
    element.appendChild(renderer.domElement);
    renderer.setClearColor('#f3f7f8');
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(48, 1, .001, 10000);
    camera.position.set(3, 2.3, 4);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = false;
    const render = () => renderer.render(scene, camera);
    controls.addEventListener('change', render);
    const cloudGeometry = new THREE.BufferGeometry();
    const cloudBuffer = new Float32Array(MAX_POINTS * 3);
    const cloudAttribute = new THREE.BufferAttribute(cloudBuffer, 3).setUsage(THREE.DynamicDrawUsage);
    cloudGeometry.setAttribute('position', cloudAttribute);
    cloudGeometry.setDrawRange(0, 0);
    const cloudMaterial = new THREE.PointsMaterial({ color: '#158b80', size: 3, sizeAttenuation: false });
    const cloud = new THREE.Points(cloudGeometry, cloudMaterial); cloud.frustumCulled = false; scene.add(cloud);
    const lineGeometry = new THREE.BufferGeometry();
    const lineBuffer = new Float32Array(MAX_POSES * 2 * 3);
    const lineAttribute = new THREE.BufferAttribute(lineBuffer, 3).setUsage(THREE.DynamicDrawUsage);
    lineGeometry.setAttribute('position', lineAttribute); lineGeometry.setDrawRange(0, 0);
    const lineMaterial = new THREE.LineBasicMaterial({ color: '#d88026' });
    const lines = new THREE.LineSegments(lineGeometry, lineMaterial); lines.frustumCulled = false; scene.add(lines);
    const poseGeometry = new THREE.BufferGeometry();
    const poseBuffer = new Float32Array(MAX_POSES * 3);
    const poseAttribute = new THREE.BufferAttribute(poseBuffer, 3).setUsage(THREE.DynamicDrawUsage);
    poseGeometry.setAttribute('position', poseAttribute); poseGeometry.setDrawRange(0, 0);
    const poseMaterial = new THREE.PointsMaterial({ color: '#b76a19', size: 3, sizeAttenuation: false });
    const poseDots = new THREE.Points(poseGeometry, poseMaterial); poseDots.frustumCulled = false; scene.add(poseDots);
    const corners = [[-.8, -.6, -1.2], [.8, -.6, -1.2], [.8, .6, -1.2], [-.8, .6, -1.2]];
    const frustum: number[] = [];
    corners.forEach((point, i) => { frustum.push(0, 0, 0, ...point, ...point, ...corners[(i + 1) % 4]); });
    const cameraGeometry = new THREE.BufferGeometry();
    cameraGeometry.setAttribute('position', new THREE.Float32BufferAttribute(frustum, 3));
    const cameraMaterial = new THREE.LineBasicMaterial({ color: '#243e73', depthTest: false });
    const glyph = new THREE.LineSegments(cameraGeometry, cameraMaterial);
    glyph.matrixAutoUpdate = false; glyph.visible = false; glyph.renderOrder = 2; scene.add(glyph);
    const axes = new THREE.AxesHelper(1); scene.add(axes);
    let current = model;
    let fitted = '';
    let radius = 1;
    const bounds = () => {
      const box = new THREE.Box3();
      boundedPoints(current.points).forEach(point => box.expandByPoint(new THREE.Vector3(...scenePoint(point.xyz))));
      current.poses.filter(pose => poseVisible(pose, current.map, current.segment))
        .forEach(pose => box.expandByPoint(new THREE.Vector3(...scenePosition(pose.Twc!))));
      return box;
    };
    const fit = () => {
      const box = bounds(); if (box.isEmpty()) return;
      const center = box.getCenter(new THREE.Vector3());
      radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, .05);
      controls.target.copy(center);
      camera.position.copy(center).add(new THREE.Vector3(1.4, 1.0, 1.6).normalize().multiplyScalar(radius * 2.8));
      camera.near = Math.max(radius / 1000, .00001); camera.far = Math.max(radius * 100, 10);
      camera.updateProjectionMatrix(); controls.update(); render();
    };
    runtime.current = {
      fit,
      update(value) {
        current = value;
        const points = boundedPoints(value.points);
        points.forEach((point, i) => cloudBuffer.set(scenePoint(point.xyz), i * 3));
        cloudGeometry.setDrawRange(0, points.length); cloudAttribute.needsUpdate = true;
        const vertices = trajectoryLines(value.poses, value.map, value.segment);
        vertices.forEach((point, i) => lineBuffer.set(point, i * 3));
        lineGeometry.setDrawRange(0, vertices.length); lineAttribute.needsUpdate = true;
        const poses = value.poses.slice(-MAX_POSES).filter(pose => poseVisible(pose, value.map, value.segment));
        poses.forEach((pose, i) => poseBuffer.set(scenePosition(pose.Twc!), i * 3));
        poseGeometry.setDrawRange(0, poses.length); poseAttribute.needsUpdate = true;
        const key = `${value.map}/${value.segment}`;
        if (fitted !== key && (points.length || poses.length)) { fit(); fitted = key; }
        axes.scale.setScalar(radius * .4);
        glyph.visible = value.camera !== null;
        if (value.camera) {
          // JSON is row-major; Three stores columns. The glyph's local forward axis is -Z.
          glyph.matrix.fromArray(scenePose(value.camera).flat()).transpose()
            .multiply(new THREE.Matrix4().makeScale(radius * .045, radius * .045, radius * .045));
          glyph.matrixWorldNeedsUpdate = true;
        }
        render();
      },
    };
    const resize = new ResizeObserver(() => {
      const width = Math.max(element.clientWidth, 1), height = Math.max(element.clientHeight, 1);
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5, Math.sqrt(2000000 / (width * height))));
      renderer.setSize(width, height, false); camera.aspect = width / height; camera.updateProjectionMatrix(); render();
    });
    resize.observe(element);
    runtime.current.update(model);
    return () => {
      runtime.current = null; resize.disconnect(); controls.removeEventListener('change', render); controls.dispose();
      [cloudGeometry, lineGeometry, poseGeometry, cameraGeometry, axes.geometry].forEach(geometry => geometry.dispose());
      [cloudMaterial, lineMaterial, poseMaterial, cameraMaterial, ...[axes.material].flat()].forEach(material => material.dispose());
      scene.clear(); renderer.dispose(); renderer.forceContextLoss(); renderer.domElement.remove();
    };
  }, []);

  useLayoutEffect(() => { runtime.current?.update(model); }, [model]);

  return <section className="card viewer" aria-label="Розріджена карта">
    <div className="card-heading"><h2>Карта й траєкторія</h2><button className="quiet" onClick={() => runtime.current?.fit()}>Вписати карту</button></div>
    <div className="map-host" ref={host}>{error && <p className="map-error" role="alert">{error}</p>}</div>
    <div className="viewer-caption"><span>Точок на екрані: <strong>{count}</strong> / {model.points.length}</span><span>Ребер траєкторії: {edges}</span></div>
    <div className="legend"><span className="dot teal" /> точки <span className="dot amber" /> траєкторія <span className="dot navy" /> камера</div>
    <p className="hint">Осі: X — червона, Y — зелена, Z — синя. Відносні одиниці. Перетягування — обертання, колесо — масштаб. Прогалини не з’єднуються.</p>
  </section>;
}

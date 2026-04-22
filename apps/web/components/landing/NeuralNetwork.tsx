"use client";

/**
 * NeuralNetwork — 3D particle network shaped as a Nefertiti bust.
 *
 * Loads farthest-point-sampled vertices from a real 3D model,
 * renders them as glowing neural-network nodes with flowing
 * connections. Three.js + custom GLSL shaders.
 */

import { useEffect, useRef, useCallback } from "react";
import * as THREE from "three";

type Pt3 = [number, number, number];

/* ── Configuration ───────────────────────────────────────────────── */

const MAX_CONN_DIST = 4.5;
const CONN_PROBABILITY = 0.25;
const MAX_CONNS_PER_NODE = 2;
const CONN_SEGMENTS = 14;
const STAR_COUNT = 3000;
const FOG_DENSITY = 0.0018;
const POINT_SIZE_SCALE = 550.0;
const ROTATION_SPEED = 0.06;
const ROTATION_ARC = 0.35;

/** Module-level cache so remounts don't re-fetch */
let pointsCache: Pt3[] | null = null;

/* ── Shared GLSL ─────────────────────────────────────────────────── */

const NOISE_GLSL = `
float hash(vec3 p) {
  p = fract(p * vec3(443.897, 441.423, 437.195));
  p += dot(p, p.yzx + 19.19);
  return fract((p.x + p.y) * p.z);
}
float noise3d(vec3 p) {
  vec3 i = floor(p);
  vec3 f = fract(p);
  f = f*f*(3.0-2.0*f);
  return mix(
    mix(mix(hash(i), hash(i+vec3(1,0,0)), f.x),
        mix(hash(i+vec3(0,1,0)), hash(i+vec3(1,1,0)), f.x), f.y),
    mix(mix(hash(i+vec3(0,0,1)), hash(i+vec3(1,0,1)), f.x),
        mix(hash(i+vec3(0,1,1)), hash(i+vec3(1,1,1)), f.x), f.y),
    f.z);
}`;

/** Shared pulse-wave calculation used by both node and connection shaders */
const PULSE_GLSL = `
float calcPulse(vec3 pos, vec3 origin, float time, float pulseTime) {
  float age = time - pulseTime;
  float radius = age * 22.0;
  float d = distance(pos, origin);
  return smoothstep(6.0, 0.0, abs(d - radius))
       * smoothstep(3.5, 0.0, age);
}`;

/* ── Shaders ─────────────────────────────────────────────────────── */

const NODE_VERT = `${NOISE_GLSL}
${PULSE_GLSL}
attribute float nodeSize;
attribute float distFromCenter;
attribute vec3 nodeColor;

uniform float uTime;
uniform vec3 uPulseOrigin;
uniform float uPulseTime;

varying vec3 vColor;
varying float vPulse;
varying float vDist;

void main() {
  vColor = nodeColor;
  vDist = distFromCenter;

  vec3 pos = position;
  float n = noise3d(pos * 0.04 + uTime * 0.04);
  pos += normal * n * 0.25;

  float breathe = sin(uTime * 0.4 + distFromCenter * 0.05) * 0.06 + 0.94;

  float pulseWave = calcPulse(pos, uPulseOrigin, uTime, uPulseTime);
  vPulse = pulseWave;

  float size = nodeSize * breathe * (1.0 + pulseWave * 2.5);

  vec4 mv = modelViewMatrix * vec4(pos, 1.0);
  gl_PointSize = size * (${POINT_SIZE_SCALE.toFixed(1)} / -mv.z);
  gl_Position = projectionMatrix * mv;
}`;

const NODE_FRAG = `
uniform float uTime;
varying vec3 vColor;
varying float vPulse;
varying float vDist;

void main() {
  vec2 c = 2.0 * gl_PointCoord - 1.0;
  float d = length(c);
  if (d > 1.0) discard;

  float glow = 1.0 - smoothstep(0.0, 0.4, d);
  float outer = 1.0 - smoothstep(0.0, 1.0, d);
  float strength = pow(glow, 1.3) + outer * 0.35;

  vec3 col = vColor * (0.9 + 0.1 * sin(uTime * 0.4 + vDist * 0.15));

  if (vPulse > 0.0) {
    col = mix(col, vec3(1.0), vPulse * 0.7);
    strength *= (1.0 + vPulse * 1.2);
  }

  col += vec3(1.0) * smoothstep(0.35, 0.0, d) * 0.35;

  float alpha = strength * (0.92 - 0.25 * d);
  gl_FragColor = vec4(col, alpha);
}`;

const CONN_VERT = `${NOISE_GLSL}
${PULSE_GLSL}
attribute vec3 startPt;
attribute vec3 endPt;
attribute float connStrength;
attribute float pathIdx;
attribute vec3 connColor;

uniform float uTime;
uniform vec3 uPulseOrigin;
uniform float uPulseTime;

varying vec3 vColor;
varying float vStrength;
varying float vPulse;
varying float vT;

void main() {
  float t = position.x;
  vT = t;

  vec3 mid = mix(startPt, endPt, 0.5);
  float arc = sin(t * 3.14159) * 0.15;
  vec3 perp = normalize(cross(normalize(endPt - startPt), vec3(0,1,0)));
  if (length(perp) < 0.01) perp = vec3(1,0,0);
  mid += perp * arc;

  vec3 p0 = mix(startPt, mid, t);
  vec3 p1 = mix(mid, endPt, t);
  vec3 finalPos = mix(p0, p1, t);

  float n = noise3d(vec3(pathIdx * 0.08, t * 0.5, uTime * 0.08));
  finalPos += perp * n * 0.1;

  vPulse = calcPulse(finalPos, uPulseOrigin, uTime, uPulseTime);
  vColor = connColor;
  vStrength = connStrength;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(finalPos, 1.0);
}`;

const CONN_FRAG = `
uniform float uTime;
varying vec3 vColor;
varying float vStrength;
varying float vPulse;
varying float vT;

void main() {
  float flow = sin(vT * 18.0 - uTime * 2.5) * 0.5 + 0.5;
  vec3 col = vColor * (0.8 + 0.2 * sin(uTime * 0.4 + vT * 8.0));

  if (vPulse > 0.0) {
    col = mix(col, vec3(1.0), vPulse * 0.6);
  }

  col *= (0.5 + flow * 0.5 * vStrength);

  float alpha = 0.25 * vStrength + flow * 0.15;
  alpha = mix(alpha, min(1.0, alpha * 2.5), vPulse);
  gl_FragColor = vec4(col, alpha);
}`;

/* ── Helpers ─────────────────────────────────────────────────────── */

const CYAN_BRIGHT = new THREE.Color(0x55ffff);
const CYAN = new THREE.Color(0x00dddd);
const CYAN_MID = new THREE.Color(0x008899);
const CYAN_DIM = new THREE.Color(0x004466);

/** Height-based color: higher normalizedY = brighter cyan. */
function nodeColor(normalizedY: number): THREE.Color {
  if (normalizedY > 0.8) return CYAN_BRIGHT.clone();
  if (normalizedY > 0.5) {
    return new THREE.Color().lerpColors(CYAN, CYAN_BRIGHT, (normalizedY - 0.5) / 0.3);
  }
  if (normalizedY > 0.2) return CYAN.clone();
  if (normalizedY > 0.05) return CYAN_MID.clone();
  return CYAN_DIM.clone();
}

function createPulseUniforms() {
  return {
    uTime: { value: 0 },
    uPulseOrigin: { value: new THREE.Vector3(1e3, 1e3, 1e3) },
    uPulseTime: { value: -100 },
  };
}

/* ── Component ───────────────────────────────────────────────────── */

export function NeuralNetwork({ className = "" }: { className?: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const stateRef = useRef<{
    renderer: THREE.WebGLRenderer;
    scene: THREE.Scene;
    camera: THREE.PerspectiveCamera;
    clock: THREE.Clock;
    nodesMat: THREE.ShaderMaterial;
    connMat: THREE.ShaderMaterial;
    starsMat: THREE.ShaderMaterial;
    frameId: number;
  } | null>(null);

  const handleClick = useCallback((e: MouseEvent) => {
    const s = stateRef.current;
    if (!s) return;
    const container = containerRef.current;
    if (!container) return;

    const rect = container.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    const y = -((e.clientY - rect.top) / rect.height) * 2 + 1;

    const ray = new THREE.Raycaster();
    ray.setFromCamera(new THREE.Vector2(x, y), s.camera);

    const plane = new THREE.Plane(
      s.camera.position.clone().normalize(),
      -s.camera.position.length() * 0.4
    );
    const pt = new THREE.Vector3();
    ray.ray.intersectPlane(plane, pt);
    if (!pt) return;

    const t = s.clock.getElapsedTime();
    s.nodesMat.uniforms.uPulseOrigin.value.copy(pt);
    s.nodesMat.uniforms.uPulseTime.value = t;
    s.connMat.uniforms.uPulseOrigin.value.copy(pt);
    s.connMat.uniforms.uPulseTime.value = t;
  }, []);

  useEffect(() => {
    const elOrNull = containerRef.current;
    if (!elOrNull) return;
    // Non-null from here — captured for async closure
    const el: HTMLDivElement = elOrNull;

    let cancelled = false;
    let cleanupFn: (() => void) | null = null;

    async function init() {
      // Load model vertices (cached after first fetch)
      if (!pointsCache) {
        try {
          const res = await fetch("/models/bust-points.json");
          pointsCache = await res.json();
        } catch {
          console.warn("NeuralNetwork: failed to load bust-points.json");
          return;
        }
      }
      const rawPoints = pointsCache!;

      if (cancelled) return;

      let minY = Infinity, maxY = -Infinity;
      for (const [, y] of rawPoints) {
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
      const yRange = maxY - minY || 1;

      const scene = new THREE.Scene();
      scene.fog = new THREE.FogExp2(0x000000, FOG_DENSITY);

      const camera = new THREE.PerspectiveCamera(
        50, el.clientWidth / el.clientHeight, 0.1, 500
      );
      // 3/4 profile — reveals nose, brow, crown
      camera.position.set(22, 4, 36);
      camera.lookAt(0, -1, -2);

      const renderer = new THREE.WebGLRenderer({
        antialias: true,
        alpha: true,
        powerPreference: "high-performance",
      });
      renderer.setSize(el.clientWidth, el.clientHeight);
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
      renderer.setClearColor(0x000000, 0);
      el.appendChild(renderer.domElement);
      renderer.domElement.style.cursor = "crosshair";

      const clock = new THREE.Clock();

      // ── Nodes ──
      const positions: number[] = [];
      const sizes: number[] = [];
      const dists: number[] = [];
      const colors: number[] = [];
      const driftDirs: number[] = [];

      for (const [x, y, z] of rawPoints) {
        positions.push(x, y, z);
        const d = Math.sqrt(x * x + y * y + z * z);
        dists.push(d);

        const ny = (y - minY) / yRange;
        sizes.push(0.22 + Math.random() * 0.28);

        const c = nodeColor(ny);
        c.offsetHSL(
          (Math.random() - 0.5) * 0.03,
          (Math.random() - 0.5) * 0.08,
          (Math.random() - 0.5) * 0.08
        );
        colors.push(c.r, c.g, c.b);

        // Random directions used by vertex shader for organic drift
        driftDirs.push(
          (Math.random() - 0.5),
          (Math.random() - 0.5),
          (Math.random() - 0.5)
        );
      }

      const nodeGeo = new THREE.BufferGeometry();
      nodeGeo.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
      nodeGeo.setAttribute("nodeSize", new THREE.Float32BufferAttribute(sizes, 1));
      nodeGeo.setAttribute("distFromCenter", new THREE.Float32BufferAttribute(dists, 1));
      nodeGeo.setAttribute("nodeColor", new THREE.Float32BufferAttribute(colors, 3));
      // Shader reads `normal` attribute for drift displacement
      nodeGeo.setAttribute("normal", new THREE.Float32BufferAttribute(driftDirs, 3));

      const nodesMat = new THREE.ShaderMaterial({
        uniforms: createPulseUniforms(),
        vertexShader: NODE_VERT,
        fragmentShader: NODE_FRAG,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });

      const nodesMesh = new THREE.Points(nodeGeo, nodesMat);
      scene.add(nodesMesh);

      // ── Connections ──
      const connPositions: number[] = [];
      const starts: number[] = [];
      const ends: number[] = [];
      const strengths: number[] = [];
      const pathIdxs: number[] = [];
      const connColors: number[] = [];
      let pathCounter = 0;

      for (let i = 0; i < rawPoints.length; i++) {
        const [ax, ay, az] = rawPoints[i];
        let nodeConns = 0;

        for (let j = i + 1; j < rawPoints.length && nodeConns < MAX_CONNS_PER_NODE; j++) {
          const [bx, by, bz] = rawPoints[j];
          const dx = ax - bx, dy = ay - by, dz = az - bz;
          const dist = Math.sqrt(dx * dx + dy * dy + dz * dz);

          if (dist < MAX_CONN_DIST && Math.random() < CONN_PROBABILITY) {
            const str = 1.0 - dist / MAX_CONN_DIST;
            const midNy = ((ay + by) / 2 - minY) / yRange;
            const c = nodeColor(midNy);

            for (let s = 0; s < CONN_SEGMENTS; s++) {
              connPositions.push(s / (CONN_SEGMENTS - 1), 0, 0);
              starts.push(ax, ay, az);
              ends.push(bx, by, bz);
              strengths.push(str);
              pathIdxs.push(pathCounter);
              connColors.push(c.r, c.g, c.b);
            }
            pathCounter++;
            nodeConns++;
          }
        }
      }

      const connGeo = new THREE.BufferGeometry();
      connGeo.setAttribute("position", new THREE.Float32BufferAttribute(connPositions, 3));
      connGeo.setAttribute("startPt", new THREE.Float32BufferAttribute(starts, 3));
      connGeo.setAttribute("endPt", new THREE.Float32BufferAttribute(ends, 3));
      connGeo.setAttribute("connStrength", new THREE.Float32BufferAttribute(strengths, 1));
      connGeo.setAttribute("pathIdx", new THREE.Float32BufferAttribute(pathIdxs, 1));
      connGeo.setAttribute("connColor", new THREE.Float32BufferAttribute(connColors, 3));

      const connMat = new THREE.ShaderMaterial({
        uniforms: createPulseUniforms(),
        vertexShader: CONN_VERT,
        fragmentShader: CONN_FRAG,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });

      const connMesh = new THREE.LineSegments(connGeo, connMat);
      scene.add(connMesh);

      // ── Starfield ──
      const starPos: number[] = [];
      const starSizes: number[] = [];
      for (let i = 0; i < STAR_COUNT; i++) {
        const r = 60 + Math.random() * 140;
        const phi = Math.acos(2 * Math.random() - 1);
        const theta = Math.random() * Math.PI * 2;
        starPos.push(
          r * Math.sin(phi) * Math.cos(theta),
          r * Math.sin(phi) * Math.sin(theta),
          r * Math.cos(phi)
        );
        starSizes.push(0.08 + Math.random() * 0.18);
      }

      const starGeo = new THREE.BufferGeometry();
      starGeo.setAttribute("position", new THREE.Float32BufferAttribute(starPos, 3));
      starGeo.setAttribute("size", new THREE.Float32BufferAttribute(starSizes, 1));

      const starsMat = new THREE.ShaderMaterial({
        uniforms: { uTime: { value: 0 } },
        vertexShader: `
          attribute float size;
          varying float vAlpha;
          uniform float uTime;
          void main() {
            vec4 mv = modelViewMatrix * vec4(position, 1.0);
            float twinkle = sin(uTime * 1.5 + position.x * 80.0) * 0.3 + 0.7;
            gl_PointSize = size * twinkle * (200.0 / -mv.z);
            gl_Position = projectionMatrix * mv;
            vAlpha = twinkle;
          }`,
        fragmentShader: `
          varying float vAlpha;
          void main() {
            float d = length(gl_PointCoord - 0.5);
            if (d > 0.5) discard;
            float a = 1.0 - smoothstep(0.0, 0.5, d);
            gl_FragColor = vec4(1.0, 1.0, 1.0, a * 0.5);
          }`,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });

      const starMesh = new THREE.Points(starGeo, starsMat);
      scene.add(starMesh);

      el.addEventListener("click", handleClick);

      function animate() {
        if (!stateRef.current) return;

        const t = clock.getElapsedTime();
        nodesMat.uniforms.uTime.value = t;
        connMat.uniforms.uTime.value = t;
        starsMat.uniforms.uTime.value = t;

        const rotY = Math.sin(t * ROTATION_SPEED) * ROTATION_ARC;
        nodesMesh.rotation.y = rotY;
        connMesh.rotation.y = rotY;
        starMesh.rotation.y += 0.00008;

        renderer.render(scene, camera);
        stateRef.current.frameId = requestAnimationFrame(animate);
      }

      stateRef.current = {
        renderer, scene, camera, clock,
        nodesMat, connMat, starsMat,
        frameId: 0,
      };
      animate();

      const onResize = () => {
        camera.aspect = el.clientWidth / el.clientHeight;
        camera.updateProjectionMatrix();
        renderer.setSize(el.clientWidth, el.clientHeight);
      };
      window.addEventListener("resize", onResize);

      cleanupFn = () => {
        el.removeEventListener("click", handleClick);
        window.removeEventListener("resize", onResize);
        cancelAnimationFrame(stateRef.current?.frameId ?? 0);
        renderer.dispose();
        nodeGeo.dispose();
        nodesMat.dispose();
        connGeo.dispose();
        connMat.dispose();
        starGeo.dispose();
        starsMat.dispose();
        if (el.contains(renderer.domElement)) {
          el.removeChild(renderer.domElement);
        }
        stateRef.current = null;
      };
    }

    init();

    return () => {
      cancelled = true;
      cleanupFn?.();
    };
  }, [handleClick]);

  return (
    <div
      ref={containerRef}
      className={`absolute inset-0 z-0 ${className}`}
      style={{ pointerEvents: "auto" }}
    />
  );
}

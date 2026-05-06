"use client";

/**
 * QuantumNetworkSection
 * ─────────────────────
 * Ambient 3D crystalline-sphere visual used as a section break on the
 * marketing homepage. Pattern-interruption between dense copy blocks
 * (Hero -> Ribbon, Versus -> Proof). Pure visual, no interaction -
 * mouse events would steal page-scroll, so we don't bind any.
 *
 * Performance discipline (the homepage is the most JS-sensitive page
 * we have - Stripe, analytics, Supabase, Recharts already load here):
 *
 *   - three/addons are imported via `await import(...)` inside a
 *     useEffect, AFTER the section enters the viewport. The initial
 *     JS bundle is unaffected; we only pay the three.js cost when
 *     the user actually scrolls past the first instance.
 *   - IntersectionObserver pauses the requestAnimationFrame loop when
 *     the canvas leaves the viewport. Two instances coexist on the
 *     page; only the visible one runs.
 *   - prefers-reduced-motion users get a static gradient SVG. No
 *     canvas, no shader compile, zero runtime cost.
 *   - Bloom strength dropped from 1.8 -> 0.8, star count 8000 ->
 *     1500, pixelRatio capped at 1.5 (was 2). Visually still rich,
 *     budget-safe on mid-tier laptops.
 *
 * Brand palette only: Oracle Gold (#d4af37), Neon Teal (#00ffff),
 * Vellum (#e8e4d8). No purple/red/blue from the upstream codepen.
 */

import { useEffect, useRef, useState } from "react";

const SECTION_HEIGHT_PX = 460;

// Brand colors as plain hex - converted to THREE.Color inside the
// effect. Centralised here so a future palette tweak is one edit.
const BRAND_PALETTE_HEX = [
    0xd4af37,  // gold
    0xe8c46a,  // gold-warm
    0x00ffff,  // teal
    0x66e6ff,  // teal-soft
    0xe8e4d8,  // vellum
];

// Connection / pulse highlight color - pulled from the same palette so
// pulses feel native to the visual.
const PULSE_COLOR_HEX = 0xffd97a;

interface Props {
    /** Visual variant. Just a label for analytics / future variants;
     *  no behaviour change today. */
    variant?: string;
    /** Override for the section min-height. Keep undefined to use
     *  the global 460px default. */
    heightPx?: number;
}

export default function QuantumNetworkSection({
    variant = "default",
    heightPx = SECTION_HEIGHT_PX,
}: Props) {
    const containerRef = useRef<HTMLDivElement | null>(null);
    const canvasRef = useRef<HTMLCanvasElement | null>(null);
    const [reducedMotion, setReducedMotion] = useState(false);
    const [inView, setInView] = useState(false);
    const inViewRef = useRef(false);

    // Detect prefers-reduced-motion on mount. We can't do this in the
    // initial render because `window` is server-undefined.
    useEffect(() => {
        const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
        const handle = () => setReducedMotion(mq.matches);
        handle();
        mq.addEventListener("change", handle);
        return () => mq.removeEventListener("change", handle);
    }, []);

    // IntersectionObserver: track whether the section is on screen.
    // Drives both the lazy three.js import (don't even fetch the
    // module until the user scrolls near the section) and the RAF
    // pause when scrolling past.
    useEffect(() => {
        if (!containerRef.current) return;
        const el = containerRef.current;
        const obs = new IntersectionObserver(
            (entries) => {
                for (const entry of entries) {
                    inViewRef.current = entry.isIntersecting;
                    setInView(entry.isIntersecting);
                }
            },
            { rootMargin: "200px 0px", threshold: 0 },
        );
        obs.observe(el);
        return () => obs.disconnect();
    }, []);

    // Three.js setup. Runs once: when the section first enters the
    // viewport AND the user doesn't have prefers-reduced-motion. The
    // RAF loop reads inViewRef every frame so we can keep it alive
    // (cheap) but skip the heavy compose() call when off-screen.
    useEffect(() => {
        if (reducedMotion) return;
        if (!inView) return;
        if (!canvasRef.current) return;

        let disposed = false;
        let raf = 0;

        const setup = async () => {
            const THREE = await import("three");
            const { OrbitControls } = await import(
                "three/addons/controls/OrbitControls.js"
            );
            const { EffectComposer } = await import(
                "three/addons/postprocessing/EffectComposer.js"
            );
            const { RenderPass } = await import(
                "three/addons/postprocessing/RenderPass.js"
            );
            const { UnrealBloomPass } = await import(
                "three/addons/postprocessing/UnrealBloomPass.js"
            );
            const { OutputPass } = await import(
                "three/addons/postprocessing/OutputPass.js"
            );

            if (disposed) return;
            const canvas = canvasRef.current;
            if (!canvas) return;

            // Scene + camera. 65deg FOV matches the codepen; the
            // section is wide-but-short so a slightly higher FOV
            // wraps the frame around the sphere nicely.
            const scene = new THREE.Scene();
            scene.fog = new THREE.FogExp2(0x05050a, 0.012);

            const w = canvas.clientWidth || 1;
            const h = canvas.clientHeight || 1;
            const camera = new THREE.PerspectiveCamera(65, w / h, 0.1, 200);
            camera.position.set(0, 4, 24);

            const renderer = new THREE.WebGLRenderer({
                canvas,
                antialias: true,
                alpha: true,
                powerPreference: "high-performance",
            });
            renderer.setSize(w, h, false);
            renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.5));
            renderer.setClearColor(0x0a0a0f, 1);
            renderer.outputColorSpace = THREE.SRGBColorSpace;

            const composer = new EffectComposer(renderer);
            composer.setSize(w, h);
            composer.addPass(new RenderPass(scene, camera));
            const bloom = new UnrealBloomPass(
                new THREE.Vector2(w, h),
                0.8,
                0.5,
                0.7,
            );
            composer.addPass(bloom);
            composer.addPass(new OutputPass());

            // Starfield - 1500 points (was 8000 in upstream codepen).
            // Still feels dense in this section's narrow viewport.
            const starfield = (() => {
                const count = 1500;
                const positions: number[] = [];
                const colors: number[] = [];
                const sizes: number[] = [];
                for (let i = 0; i < count; i++) {
                    const r = THREE.MathUtils.randFloat(40, 110);
                    const phi = Math.acos(THREE.MathUtils.randFloatSpread(2));
                    const theta = THREE.MathUtils.randFloat(0, Math.PI * 2);
                    positions.push(
                        r * Math.sin(phi) * Math.cos(theta),
                        r * Math.sin(phi) * Math.sin(theta),
                        r * Math.cos(phi),
                    );
                    // Bias toward warm cream + gold tints (vellum +
                    // gold) instead of cool whites to read as Delfi.
                    const c = Math.random();
                    if (c < 0.55) colors.push(0.91, 0.89, 0.84);     // vellum-ish
                    else if (c < 0.85) colors.push(0.83, 0.69, 0.22); // gold
                    else colors.push(0.5, 0.95, 1.0);                 // teal accent
                    sizes.push(THREE.MathUtils.randFloat(0.08, 0.22));
                }
                const geo = new THREE.BufferGeometry();
                geo.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
                geo.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
                geo.setAttribute("size", new THREE.Float32BufferAttribute(sizes, 1));
                const mat = new THREE.ShaderMaterial({
                    uniforms: { uTime: { value: 0 } },
                    vertexShader: `
                        attribute float size;
                        attribute vec3 color;
                        varying vec3 vColor;
                        uniform float uTime;
                        void main() {
                            vColor = color;
                            vec4 mv = modelViewMatrix * vec4(position, 1.0);
                            float twinkle = sin(uTime * 1.3 + position.x * 80.0) * 0.3 + 0.7;
                            gl_PointSize = size * twinkle * (300.0 / -mv.z);
                            gl_Position = projectionMatrix * mv;
                        }
                    `,
                    fragmentShader: `
                        varying vec3 vColor;
                        void main() {
                            vec2 c = gl_PointCoord - 0.5;
                            float d = length(c);
                            if (d > 0.5) discard;
                            float a = 1.0 - smoothstep(0.0, 0.5, d);
                            gl_FragColor = vec4(vColor, a * 0.7);
                        }
                    `,
                    transparent: true,
                    depthWrite: false,
                    blending: THREE.AdditiveBlending,
                });
                return new THREE.Points(geo, mat);
            })();
            scene.add(starfield);

            // Crystalline-sphere generator - upstream's "formation 0",
            // simplified to one canonical shape. Density factor 0.7
            // keeps the node count manageable for two coexisting
            // instances.
            type Node = {
                position: THREE.Vector3;
                connections: { node: Node; strength: number }[];
                level: number;
                type: number;
                size: number;
                distanceFromRoot: number;
            };
            const buildNetwork = (densityFactor = 0.7) => {
                const nodes: Node[] = [];
                const root: Node = {
                    position: new THREE.Vector3(0, 0, 0),
                    connections: [], level: 0, type: 0, size: 1.6,
                    distanceFromRoot: 0,
                };
                nodes.push(root);
                const layers = 4;
                const phi = (1 + Math.sqrt(5)) / 2;

                for (let layer = 1; layer <= layers; layer++) {
                    const radius = layer * 4;
                    const numPoints = Math.floor(layer * 10 * densityFactor);
                    for (let i = 0; i < numPoints; i++) {
                        const phiAngle = Math.acos(1 - 2 * (i + 0.5) / numPoints);
                        const theta = 2 * Math.PI * i / phi;
                        const pos = new THREE.Vector3(
                            radius * Math.sin(phiAngle) * Math.cos(theta),
                            radius * Math.sin(phiAngle) * Math.sin(theta),
                            radius * Math.cos(phiAngle),
                        );
                        const isLeaf = layer === layers || Math.random() < 0.3;
                        const node: Node = {
                            position: pos,
                            connections: [],
                            level: layer,
                            type: isLeaf ? 1 : 0,
                            size: isLeaf
                                ? THREE.MathUtils.randFloat(0.4, 0.8)
                                : THREE.MathUtils.randFloat(0.7, 1.2),
                            distanceFromRoot: radius,
                        };
                        nodes.push(node);
                        if (layer > 1) {
                            const prev = nodes
                                .filter((n) => n.level === layer - 1 && n !== root)
                                .sort((a, b) =>
                                    pos.distanceTo(a.position) - pos.distanceTo(b.position));
                            for (let j = 0; j < Math.min(2, prev.length); j++) {
                                const dist = pos.distanceTo(prev[j].position);
                                const s = Math.max(0.3, 1 - dist / (radius * 2));
                                node.connections.push({ node: prev[j], strength: s });
                                prev[j].connections.push({ node, strength: s });
                            }
                        } else {
                            node.connections.push({ node: root, strength: 0.9 });
                            root.connections.push({ node, strength: 0.9 });
                        }
                    }
                }
                return nodes;
            };

            // Brand palette as THREE.Color objects.
            const palette = BRAND_PALETTE_HEX.map((hex) => new THREE.Color(hex));
            // pulseColor reserved for future click-to-pulse variant; not
            // bound today (no mouse events on this section).
            void PULSE_COLOR_HEX;

            const networkNodes = buildNetwork();

            // Build node geometry + materials. Same shader strategy as
            // upstream: per-point shader that handles node type, size
            // breathing, and idle glow.
            const nodesGeo = new THREE.BufferGeometry();
            const nPos: number[] = [];
            const nType: number[] = [];
            const nSize: number[] = [];
            const nColor: number[] = [];
            const nDist: number[] = [];
            for (const node of networkNodes) {
                nPos.push(node.position.x, node.position.y, node.position.z);
                nType.push(node.type);
                nSize.push(node.size);
                nDist.push(node.distanceFromRoot);
                const idx = Math.min(node.level, palette.length - 1);
                const base = palette[idx % palette.length].clone();
                base.offsetHSL(
                    THREE.MathUtils.randFloatSpread(0.02),
                    THREE.MathUtils.randFloatSpread(0.05),
                    THREE.MathUtils.randFloatSpread(0.06),
                );
                nColor.push(base.r, base.g, base.b);
            }
            nodesGeo.setAttribute("position", new THREE.Float32BufferAttribute(nPos, 3));
            nodesGeo.setAttribute("nodeType", new THREE.Float32BufferAttribute(nType, 1));
            nodesGeo.setAttribute("nodeSize", new THREE.Float32BufferAttribute(nSize, 1));
            nodesGeo.setAttribute("nodeColor", new THREE.Float32BufferAttribute(nColor, 3));
            nodesGeo.setAttribute("distanceFromRoot",
                new THREE.Float32BufferAttribute(nDist, 1));

            const nodesMat = new THREE.ShaderMaterial({
                uniforms: {
                    uTime: { value: 0 },
                    uBaseSize: { value: 0.55 },
                },
                vertexShader: `
                    attribute float nodeSize;
                    attribute float nodeType;
                    attribute vec3 nodeColor;
                    attribute float distanceFromRoot;
                    uniform float uTime;
                    uniform float uBaseSize;
                    varying vec3 vColor;
                    varying float vNodeType;
                    varying float vDistanceFromRoot;
                    void main() {
                        vColor = nodeColor;
                        vNodeType = nodeType;
                        vDistanceFromRoot = distanceFromRoot;
                        float breathe = sin(uTime * 0.6 + distanceFromRoot * 0.15) * 0.15 + 0.85;
                        vec4 mv = modelViewMatrix * vec4(position, 1.0);
                        gl_PointSize = nodeSize * breathe * uBaseSize * (1000.0 / -mv.z);
                        gl_Position = projectionMatrix * mv;
                    }
                `,
                fragmentShader: `
                    uniform float uTime;
                    varying vec3 vColor;
                    varying float vNodeType;
                    varying float vDistanceFromRoot;
                    void main() {
                        vec2 c = 2.0 * gl_PointCoord - 1.0;
                        float d = length(c);
                        if (d > 1.0) discard;
                        float glow1 = 1.0 - smoothstep(0.0, 0.5, d);
                        float glow2 = 1.0 - smoothstep(0.0, 1.0, d);
                        float strength = pow(glow1, 1.2) + glow2 * 0.3;
                        float breathe = 0.9 + 0.1 * sin(uTime * 0.5 + vDistanceFromRoot * 0.25);
                        vec3 final = vColor * breathe;
                        float core = smoothstep(0.4, 0.0, d);
                        final += vec3(1.0) * core * 0.25;
                        float alpha = strength * (0.95 - 0.3 * d);
                        gl_FragColor = vec4(final, alpha);
                    }
                `,
                transparent: true,
                depthWrite: false,
                blending: THREE.AdditiveBlending,
            });
            const nodesMesh = new THREE.Points(nodesGeo, nodesMat);
            scene.add(nodesMesh);

            // Build connection geometry. We segment each connection
            // into 16 vertices (was 20 upstream); the shader morphs
            // them into a slight curve so straight lines aren't
            // boring. Per-pixel flow + hue breathing in the fragment
            // shader gives the "synapse firing" feel without any
            // scripted pulse dispatch.
            const connGeo = new THREE.BufferGeometry();
            const cPos: number[] = [];
            const cStart: number[] = [];
            const cEnd: number[] = [];
            const cIdx: number[] = [];
            const cStrength: number[] = [];
            const cColor: number[] = [];
            const seen = new Set<string>();
            let pathIdx = 0;
            networkNodes.forEach((n, idx) => {
                for (const conn of n.connections) {
                    const otherIdx = networkNodes.indexOf(conn.node);
                    if (otherIdx === -1) continue;
                    const key = idx < otherIdx ? `${idx}-${otherIdx}` : `${otherIdx}-${idx}`;
                    if (seen.has(key)) continue;
                    seen.add(key);
                    const SEG = 16;
                    for (let s = 0; s < SEG; s++) {
                        const t = s / (SEG - 1);
                        cPos.push(t, 0, 0);
                        cStart.push(n.position.x, n.position.y, n.position.z);
                        cEnd.push(conn.node.position.x, conn.node.position.y, conn.node.position.z);
                        cIdx.push(pathIdx);
                        cStrength.push(conn.strength);
                        const lvl = Math.min(
                            Math.floor((n.level + conn.node.level) / 2),
                            palette.length - 1,
                        );
                        const base = palette[lvl % palette.length].clone();
                        base.offsetHSL(
                            THREE.MathUtils.randFloatSpread(0.02),
                            THREE.MathUtils.randFloatSpread(0.05),
                            THREE.MathUtils.randFloatSpread(0.06),
                        );
                        cColor.push(base.r, base.g, base.b);
                    }
                    pathIdx++;
                }
            });
            connGeo.setAttribute("position", new THREE.Float32BufferAttribute(cPos, 3));
            connGeo.setAttribute("startPoint", new THREE.Float32BufferAttribute(cStart, 3));
            connGeo.setAttribute("endPoint", new THREE.Float32BufferAttribute(cEnd, 3));
            connGeo.setAttribute("pathIndex", new THREE.Float32BufferAttribute(cIdx, 1));
            connGeo.setAttribute("connectionStrength",
                new THREE.Float32BufferAttribute(cStrength, 1));
            connGeo.setAttribute("connectionColor",
                new THREE.Float32BufferAttribute(cColor, 3));

            const connMat = new THREE.ShaderMaterial({
                uniforms: {
                    uTime: { value: 0 },
                },
                vertexShader: `
                    attribute vec3 startPoint;
                    attribute vec3 endPoint;
                    attribute float connectionStrength;
                    attribute float pathIndex;
                    attribute vec3 connectionColor;
                    uniform float uTime;
                    varying vec3 vColor;
                    varying float vConnectionStrength;
                    varying float vPathPosition;
                    void main() {
                        float t = position.x;
                        vPathPosition = t;
                        vec3 mid = mix(startPoint, endPoint, 0.5);
                        float curve = sin(t * 3.14159) * 0.13;
                        vec3 dir = normalize(endPoint - startPoint);
                        vec3 perp = normalize(cross(dir, vec3(0.0, 1.0, 0.0)));
                        if (length(perp) < 0.1) perp = vec3(1.0, 0.0, 0.0);
                        mid += perp * curve;
                        vec3 p0 = mix(startPoint, mid, t);
                        vec3 p1 = mix(mid, endPoint, t);
                        vec3 finalPos = mix(p0, p1, t);
                        // Subtle drift over time so connections breathe.
                        finalPos += perp * sin(uTime * 0.4 + pathIndex * 0.3) * 0.05;
                        vColor = connectionColor;
                        vConnectionStrength = connectionStrength;
                        gl_Position = projectionMatrix * modelViewMatrix * vec4(finalPos, 1.0);
                    }
                `,
                fragmentShader: `
                    uniform float uTime;
                    varying vec3 vColor;
                    varying float vConnectionStrength;
                    varying float vPathPosition;
                    void main() {
                        float flow1 = sin(vPathPosition * 22.0 - uTime * 3.0) * 0.5 + 0.5;
                        float flow2 = sin(vPathPosition * 12.0 - uTime * 2.0 + 1.57) * 0.5 + 0.5;
                        float flow = (flow1 + flow2 * 0.5) / 1.5;
                        vec3 base = vColor * (0.85 + 0.15 * sin(uTime * 0.5 + vPathPosition * 10.0));
                        float intensity = 0.45 * flow * vConnectionStrength;
                        vec3 final = base * (0.7 + intensity + vConnectionStrength * 0.4);
                        float alpha = 0.55 * vConnectionStrength + flow * 0.25;
                        gl_FragColor = vec4(final, alpha);
                    }
                `,
                transparent: true,
                depthWrite: false,
                blending: THREE.AdditiveBlending,
            });
            const connMesh = new THREE.LineSegments(connGeo, connMat);
            scene.add(connMesh);

            // Auto-rotate-only controls. enablePan/zoom off so accidental
            // touch-drags can't move the section out of frame.
            const controls = new OrbitControls(camera, renderer.domElement);
            controls.enableDamping = true;
            controls.dampingFactor = 0.05;
            controls.enablePan = false;
            controls.enableZoom = false;
            controls.enableRotate = false;
            controls.autoRotate = true;
            controls.autoRotateSpeed = 0.35;

            // Resize handling. ResizeObserver is more reliable than
            // window.resize for the case where a parent layout shifts
            // (responsive grid breakpoints, font loading reflow, etc).
            const resize = () => {
                if (!canvas) return;
                const cw = canvas.clientWidth || 1;
                const ch = canvas.clientHeight || 1;
                camera.aspect = cw / ch;
                camera.updateProjectionMatrix();
                renderer.setSize(cw, ch, false);
                composer.setSize(cw, ch);
                bloom.resolution.set(cw, ch);
            };
            const ro = new ResizeObserver(resize);
            ro.observe(canvas);

            const start = performance.now();
            const tick = () => {
                if (disposed) return;
                raf = requestAnimationFrame(tick);
                // RAF is cheap; the expensive bit is the compose() call.
                // Skip it when off-screen.
                if (!inViewRef.current) return;
                const t = (performance.now() - start) / 1000;
                (starfield.material as THREE.ShaderMaterial).uniforms.uTime.value = t;
                nodesMat.uniforms.uTime.value = t;
                connMat.uniforms.uTime.value = t;
                starfield.rotation.y = t * 0.005;
                nodesMesh.rotation.y = Math.sin(t * 0.04) * 0.05;
                connMesh.rotation.y = Math.sin(t * 0.04) * 0.05;
                controls.update();
                composer.render();
            };
            tick();

            return () => {
                cancelAnimationFrame(raf);
                ro.disconnect();
                controls.dispose();
                composer.dispose();
                renderer.dispose();
                nodesGeo.dispose();
                nodesMat.dispose();
                connGeo.dispose();
                connMat.dispose();
                (starfield.geometry as THREE.BufferGeometry).dispose();
                (starfield.material as THREE.ShaderMaterial).dispose();
                scene.clear();
            };
        };

        let cleanup: (() => void) | undefined;
        setup().then((c) => {
            if (disposed) {
                if (c) c();
                return;
            }
            cleanup = c;
        }).catch((err) => {
            console.error("[QuantumNetworkSection] setup failed:", err);
        });

        return () => {
            disposed = true;
            cancelAnimationFrame(raf);
            if (cleanup) cleanup();
        };
        // We deliberately re-run the effect when inView toggles from
        // false->true (first time the user scrolls to the section).
        // Subsequent toggles are absorbed by inViewRef inside tick().
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [inView, reducedMotion]);

    // Reduced-motion fallback: a static gradient SVG that matches the
    // brand colors. No Three.js, no shader compile, no canvas - the
    // section still breaks the visual pattern but costs zero runtime.
    if (reducedMotion) {
        return (
            <section
                aria-hidden="true"
                style={{
                    minHeight: heightPx,
                    width: "100%",
                    background:
                        "radial-gradient(ellipse at 50% 50%, rgba(212,175,55,0.15) 0%, " +
                        "rgba(0,255,255,0.08) 35%, rgba(10,10,15,1) 75%)",
                    pointerEvents: "none",
                }}
                data-quantum-variant={variant}
                data-quantum-mode="reduced-motion"
            />
        );
    }

    return (
        <section
            ref={containerRef}
            aria-hidden="true"
            style={{
                minHeight: heightPx,
                width: "100%",
                position: "relative",
                overflow: "hidden",
                pointerEvents: "none",
                background:
                    "radial-gradient(ellipse at 50% 50%, rgba(212,175,55,0.06) 0%, " +
                    "rgba(10,10,15,1) 65%)",
            }}
            data-quantum-variant={variant}
        >
            <canvas
                ref={canvasRef}
                style={{
                    display: "block",
                    width: "100%",
                    height: "100%",
                    position: "absolute",
                    inset: 0,
                }}
            />
            {/* Top + bottom edge fades so the section dissolves into
                the obsidian above and below it without a hard line. */}
            <div
                aria-hidden
                style={{
                    position: "absolute",
                    inset: 0,
                    pointerEvents: "none",
                    background:
                        "linear-gradient(to bottom, " +
                        "rgba(10,10,15,1) 0%, " +
                        "rgba(10,10,15,0) 12%, " +
                        "rgba(10,10,15,0) 88%, " +
                        "rgba(10,10,15,1) 100%)",
                }}
            />
        </section>
    );
}

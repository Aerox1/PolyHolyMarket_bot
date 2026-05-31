import { useState, type ReactNode } from "react";
import {
  motion,
  type PanInfo,
  AnimatePresence,
} from "framer-motion";

export type Dir = "up" | "down" | "left" | "right";

interface Props {
  // Unique key for the current card so AnimatePresence animates swaps.
  cardKey: string | number;
  background: string; // CSS background shorthand (gradient or image)
  children: ReactNode;
  // Which gestures are enabled at this level.
  onSwipe: (dir: Dir) => void;
  enable: Partial<Record<Dir, boolean>>;
  // Animate the OUTGOING card in this direction (set just before key change).
  exitDir?: Dir | null;
}

const OFFSET_FRACTION = 0.25; // 25% of the relevant screen dimension
const VELOCITY_THRESHOLD = 500; // px/s

function exitVariant(dir: Dir | null | undefined) {
  switch (dir) {
    case "up":
      return { y: "-100%", opacity: 0 };
    case "down":
      return { y: "100%", opacity: 0 };
    case "left":
      return { x: "-100%", opacity: 0 };
    case "right":
      return { x: "100%", opacity: 0 };
    default:
      return { opacity: 0, scale: 0.96 };
  }
}

function enterVariant(dir: Dir | null | undefined) {
  // New card enters from the opposite edge of the swipe.
  switch (dir) {
    case "up":
      return { y: "100%", opacity: 0.4 };
    case "down":
      return { y: "-100%", opacity: 0.4 };
    case "left":
      return { x: "100%", opacity: 0.4 };
    case "right":
      return { x: "-100%", opacity: 0.4 };
    default:
      return { opacity: 0, scale: 1.02 };
  }
}

export function SwipeCard({
  cardKey,
  background,
  children,
  onSwipe,
  enable,
  exitDir,
}: Props) {
  const [dragDir, setDragDir] = useState<Dir | null>(null);

  function handleDragEnd(_e: unknown, info: PanInfo) {
    const w = window.innerWidth;
    const h = window.innerHeight;
    const { offset, velocity } = info;

    const horizontal = Math.abs(offset.x) > Math.abs(offset.y);

    if (horizontal) {
      const passed =
        Math.abs(offset.x) > w * OFFSET_FRACTION ||
        Math.abs(velocity.x) > VELOCITY_THRESHOLD;
      if (passed) {
        const dir: Dir = offset.x < 0 ? "left" : "right";
        if (enable[dir]) {
          onSwipe(dir);
          return;
        }
      }
    } else {
      const passed =
        Math.abs(offset.y) > h * OFFSET_FRACTION ||
        Math.abs(velocity.y) > VELOCITY_THRESHOLD;
      if (passed) {
        const dir: Dir = offset.y < 0 ? "up" : "down";
        if (enable[dir]) {
          onSwipe(dir);
          return;
        }
      }
    }
    setDragDir(null); // spring back
  }

  return (
    <AnimatePresence initial={false} mode="popLayout" custom={exitDir}>
      <motion.div
        key={cardKey}
        className="card"
        style={{ background }}
        custom={exitDir}
        drag
        dragDirectionLock
        dragElastic={0.55}
        dragConstraints={{ left: 0, right: 0, top: 0, bottom: 0 }}
        onDrag={(_e, info) => {
          const horizontal = Math.abs(info.offset.x) > Math.abs(info.offset.y);
          setDragDir(
            horizontal
              ? info.offset.x < 0
                ? "left"
                : "right"
              : info.offset.y < 0
                ? "up"
                : "down",
          );
        }}
        onDragEnd={handleDragEnd}
        initial={enterVariant(exitDir)}
        animate={{ x: 0, y: 0, opacity: 1, scale: 1 }}
        exit={exitVariant(exitDir)}
        transition={{ type: "spring", stiffness: 380, damping: 38 }}
        data-drag-dir={dragDir ?? undefined}
      >
        {children}
      </motion.div>
    </AnimatePresence>
  );
}

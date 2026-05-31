import type { ReactNode } from "react";
import { motion } from "framer-motion";

// Bottom-sheet shell shared by the portfolio + leaderboard sheets. Mirrors the
// markup/animation BetPanel uses inline (backdrop + spring-up panel + handle).
export function Sheet({
  children,
  onClose,
}: {
  children: ReactNode;
  onClose: () => void;
}) {
  return (
    <>
      <motion.div
        className="sheet-backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        onClick={onClose}
      />
      <motion.div
        className="sheet"
        initial={{ y: "100%" }}
        animate={{ y: 0 }}
        exit={{ y: "100%" }}
        transition={{ type: "spring", stiffness: 360, damping: 36 }}
      >
        <div className="sheet-handle" />
        {children}
      </motion.div>
    </>
  );
}

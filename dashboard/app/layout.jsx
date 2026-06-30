import "./globals.css";

export const metadata = {
  title: "SiliconKnights · Causal AIOps",
  description: "Causal correlation verdict for a single-node Kubernetes factory.",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

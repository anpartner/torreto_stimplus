import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "AI Ecommerce Search",
  description: "Interface conversationnelle pour un moteur de recherche e-commerce hybride."
};

export default function RootLayout({
  children
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="fr">
      <body>{children}</body>
    </html>
  );
}

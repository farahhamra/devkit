from __future__ import annotations

import textwrap

import pytest


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVKIT_CONFIG_DIR", raising=False)
    files = {
        "devkit/electroerp.yaml": """
            apps:
              erp:
                roots: [backend, frontend]
                schemas: [backend/prisma/schema.prisma]
              hub:
                roots: [hub]
                schemas: []
            modules:
              products:
                app: erp
                aliases: [product, product-variants]
                models: [Product, ProductVariant]
                paths: []
                required: [backend/src/core/features/products/product.service.ts]
              organizations:
                models: [Organization]
              invoices:
                models: [Invoice]
            shared_models: [Organization]
        """,
        "backend/prisma/schema.prisma": """
            generator client { provider = "prisma-client-js" }
            datasource db { provider = "mysql" url = env("DATABASE_URL") }
            model Organization {
              id Int @id @default(autoincrement())
              products Product[]
              invoices Invoice[]
              @@map("organizations")
            }
            model Product {
              id Int @id @default(autoincrement())
              organizationId Int @map("organization_id")
              name String @db.VarChar(255)
              baseSellingPrice Decimal? @map("base_selling_price") @db.Decimal(12, 2)
              status ProductStatus @default(active)
              organization Organization @relation(
                fields: [organizationId],
                references: [id],
                onDelete: Cascade
              )
              variants ProductVariant[]
              invoices Invoice[]
              @@index([organizationId])
              @@unique([organizationId, name], name: "org_name")
              @@map("products")
            }
            model ProductVariant {
              id Int @id @default(autoincrement())
              productId Int
              product Product @relation(fields: [productId], references: [id], onDelete: Cascade)
              @@map("product_variants")
            }
            model Invoice {
              id Int @id
              productId Int
              organizationId Int
              product Product @relation(fields: [productId], references: [id], onDelete: Restrict)
              organization Organization @relation(fields: [organizationId], references: [id])
              @@map("invoices")
            }
            enum ProductStatus {
              active
              archived @map("inactive")
            }
        """,
        "backend/src/app.ts": """
            import express from 'express';
            import products from './core/features/products/product.routes.js';
            const app = express();
            app.use('/api/products', products);
        """,
        "backend/src/core/features/products/product.routes.ts": """
            import { Router } from 'express';
            import { getProducts, updateProduct } from './product.controller';
            const router = Router();
            router.get('/', auth, getProducts);
            router.patch('/:id', auth, updateProduct);
            export default router;
        """,
        "backend/src/core/features/products/product.controller.ts": """
            import { listProducts as loadProducts } from './product.service';
            export async function getProducts(req, res) { return res.json(await loadProducts()); }
            export const updateProduct = (req, res) => res.json(req.body);
        """,
        "backend/src/core/features/products/product.service.ts": """
            import { PrismaClient } from '@prisma/client';
            const prisma = new PrismaClient();
            export async function listProducts() {
              return prisma.product.findMany({ select: { id: true, baseSellingPrice: true } });
            }
            export interface ProductDto { id: number; baseSellingPrice: number }
            export type ProductId = number;
            export enum Status { Active, Archived }
            export class ProductService {
              list() { return listProducts(); }
            }
            function localOnly() { return 'literal[brackets]'; }
        """,
        "backend/src/core/features/products/product.types.ts": "export interface ProductInput { name: string }",
        "backend/src/core/features/products/product.validators.ts": "export const validateProduct = (x: unknown) => Boolean(x);",
        "backend/src/core/features/invoices/invoice.service.ts": """
            import { listProducts } from '../products/product.service';
            export const invoiceProducts = () => listProducts();
        """,
        "backend/src/core/features/products/index.ts": "export { listProducts as loadProducts } from './product.service';",
        "backend/src/core/features/ai/tools/products/read-products.tool.ts": "export const readProducts = () => 'products';",
        "backend/ai/rag/tools/products/read-products.tool.md": "# Read products\nReads the Product model.",
        "backend/prisma/migrations/001/migration.sql": "ALTER TABLE `products` ADD COLUMN `base_selling_price` DECIMAL(12,2);",
        "frontend/src/app/core/products/api/products.api.ts": """
            export const getProducts = () => fetch('/api/products/');
        """,
        "frontend/src/app/core/products/hooks/useProducts.ts": """
            import { getProducts } from '@/app/core/products/api/products.api';
            export const useProducts = () => getProducts();
        """,
        "frontend/src/app/core/products/components/ProductForm.tsx": """
            import { useProducts } from '../hooks/useProducts';
            export const ProductForm = () => <div>{useProducts().baseSellingPrice}</div>;
        """,
        "frontend/src/lib/unrelated.ts": "export const readOther = (other) => other.baseSellingPrice;",
        "frontend/src/.visible.ts": "export const visible = 'specialneedle';",
        "hub/src/index.ts": "export const hubOnly = 'hubneedle';",
        "notes.txt": "Token token TOKEN\nliteral[brackets]\nspecialneedle\n",
        ".gitignore": "notes.txt\nfrontend/src/.visible.ts\n",
        ".env": "DO_NOT_SHOW=supersecretneedle",
        "backend/node_modules/dependency/index.js": "export const hidden = 'supersecretneedle';",
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    (tmp_path / "binary.dat").write_bytes(b"\0supersecretneedle")
    return tmp_path

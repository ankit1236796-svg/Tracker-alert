import aiosqlite

from config import DATABASE_NAME


class Database:

    async def connect(self):
        self.db = await aiosqlite.connect(DATABASE_NAME)

        await self.db.execute("""
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL UNIQUE,
            website TEXT NOT NULL,
            stock INTEGER DEFAULT 0
        )
        """)

        await self.db.commit()


    async def add_product(self, name, url, website):
        await self.db.execute(
            """
            INSERT INTO products(name,url,website)
            VALUES(?,?,?)
            """,
            (name, url, website)
        )

        await self.db.commit()


    async def get_products(self):

        cursor = await self.db.execute(
            """
            SELECT id,name,url,website,stock
            FROM products
            """
        )

        return await cursor.fetchall()


    async def delete_product(self, product_id):

        await self.db.execute(
            """
            DELETE FROM products
            WHERE id=?
            """,
            (product_id,)
        )

        await self.db.commit()


    async def update_stock(self, product_id, stock):

        await self.db.execute(
            """
            UPDATE products
            SET stock=?
            WHERE id=?
            """,
            (stock, product_id)
        )

        await self.db.commit()

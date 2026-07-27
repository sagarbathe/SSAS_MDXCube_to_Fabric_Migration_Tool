/* =====================================================================
   Simple Retail Star Schema for SSAS Multidimensional Demo
   Database: RetailDW
   Fact: FactSales
   Dimensions: DimDate, DimProduct, DimCustomer
   ===================================================================== */

IF DB_ID('RetailDW') IS NULL
BEGIN
    CREATE DATABASE RetailDW;
END
GO

USE RetailDW;
GO

/* ---------- Drop existing objects (safe re-run) ---------- */
IF OBJECT_ID('dbo.FactSales', 'U') IS NOT NULL DROP TABLE dbo.FactSales;
IF OBJECT_ID('dbo.DimDate', 'U') IS NOT NULL DROP TABLE dbo.DimDate;
IF OBJECT_ID('dbo.DimProduct', 'U') IS NOT NULL DROP TABLE dbo.DimProduct;
IF OBJECT_ID('dbo.DimCustomer', 'U') IS NOT NULL DROP TABLE dbo.DimCustomer;
GO

/* ---------- Dimension: Date ---------- */
CREATE TABLE dbo.DimDate
(
    DateKey     INT         NOT NULL PRIMARY KEY,   -- yyyymmdd
    FullDate    DATE        NOT NULL,
    DayOfMonth  TINYINT     NOT NULL,
    MonthNumber TINYINT     NOT NULL,
    MonthName   VARCHAR(20) NOT NULL,
    Quarter     TINYINT     NOT NULL,
    Year        SMALLINT    NOT NULL
);
GO

/* ---------- Dimension: Product ---------- */
CREATE TABLE dbo.DimProduct
(
    ProductKey  INT          NOT NULL PRIMARY KEY IDENTITY(1,1),
    ProductName VARCHAR(100) NOT NULL,
    Category    VARCHAR(50)  NOT NULL,
    SubCategory VARCHAR(50)  NOT NULL,
    UnitPrice   DECIMAL(10,2) NOT NULL
);
GO

/* ---------- Dimension: Customer ---------- */
CREATE TABLE dbo.DimCustomer
(
    CustomerKey  INT          NOT NULL PRIMARY KEY IDENTITY(1,1),
    CustomerName VARCHAR(100) NOT NULL,
    City         VARCHAR(50)  NOT NULL,
    Country      VARCHAR(50)  NOT NULL
);
GO

/* ---------- Fact: Sales ---------- */
CREATE TABLE dbo.FactSales
(
    SalesID      INT IDENTITY(1,1) NOT NULL PRIMARY KEY,
    DateKey      INT NOT NULL REFERENCES dbo.DimDate(DateKey),
    ProductKey   INT NOT NULL REFERENCES dbo.DimProduct(ProductKey),
    CustomerKey  INT NOT NULL REFERENCES dbo.DimCustomer(CustomerKey),
    Quantity     INT NOT NULL,
    SalesAmount  DECIMAL(12,2) NOT NULL,
    CostAmount   DECIMAL(12,2) NOT NULL
);
GO

/* ---------- Sample Data ---------- */
INSERT INTO dbo.DimDate (DateKey, FullDate, DayOfMonth, MonthNumber, MonthName, Quarter, Year)
VALUES
(20240101,'2024-01-01',1,1,'January',1,2024),
(20240115,'2024-01-15',15,1,'January',1,2024),
(20240201,'2024-02-01',1,2,'February',1,2024),
(20240301,'2024-03-01',1,3,'March',1,2024),
(20240401,'2024-04-01',1,4,'April',2,2024),
(20240501,'2024-05-01',1,5,'May',2,2024);

INSERT INTO dbo.DimProduct (ProductName, Category, SubCategory, UnitPrice)
VALUES
('Road Bike 100',   'Bikes',       'Road Bikes',    899.99),
('Mountain Bike 200','Bikes',      'Mountain Bikes',1099.99),
('Helmet Basic',    'Accessories', 'Helmets',        39.99),
('Water Bottle',    'Accessories', 'Bottles',         5.99),
('Bike Gloves',     'Clothing',    'Gloves',         19.99);

INSERT INTO dbo.DimCustomer (CustomerName, City, Country)
VALUES
('Alice Johnson', 'Seattle',   'USA'),
('Bob Smith',     'Chicago',   'USA'),
('Carla Diaz',    'Madrid',    'Spain'),
('David Kim',     'Seoul',     'South Korea'),
('Emma Brown',    'London',    'UK');

INSERT INTO dbo.FactSales (DateKey, ProductKey, CustomerKey, Quantity, SalesAmount, CostAmount)
VALUES
(20240101, 1, 1, 1,  899.99, 600.00),
(20240115, 3, 1, 2,   79.98,  30.00),
(20240201, 2, 2, 1, 1099.99, 750.00),
(20240301, 4, 3, 3,   17.97,   6.00),
(20240401, 5, 4, 1,   19.99,   8.00),
(20240501, 1, 5, 1,  899.99, 600.00),
(20240501, 3, 2, 1,   39.99,  15.00);
GO

PRINT 'RetailDW star schema created and populated successfully.';

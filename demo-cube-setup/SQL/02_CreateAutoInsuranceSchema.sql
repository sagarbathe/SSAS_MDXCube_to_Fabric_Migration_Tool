/*
  Auto Insurance Claims Star Schema
  ---------------------------------
  Database : AutoInsuranceDW
  Fact      : Fact_Claims (~100 claim records)
  Dimensions: Dim_Date, Dim_Policyholder (20), Dim_Policy (50, linked to policyholder),
              Dim_ClaimType, Dim_Geography (with Latitude/Longitude)
  Period    : 3 years (2023-01-01 .. 2025-12-31)
*/

IF DB_ID('AutoInsuranceDW') IS NOT NULL
BEGIN
    ALTER DATABASE AutoInsuranceDW SET SINGLE_USER WITH ROLLBACK IMMEDIATE;
    DROP DATABASE AutoInsuranceDW;
END
GO

CREATE DATABASE AutoInsuranceDW;
GO

USE AutoInsuranceDW;
GO

------------------------------------------------------------
-- Dim_Date
------------------------------------------------------------
CREATE TABLE dbo.Dim_Date
(
    DateKey        INT         NOT NULL PRIMARY KEY,   -- yyyymmdd
    FullDate       DATE        NOT NULL,
    [Year]         INT         NOT NULL,
    [Quarter]      INT         NOT NULL,
    [Month]        INT         NOT NULL,
    MonthName      VARCHAR(20) NOT NULL,
    [Day]          INT         NOT NULL,
    DayOfWeek      INT         NOT NULL,
    WeekdayName    VARCHAR(20) NOT NULL,
    -- Self-referencing "period rollup" key used to build a genuine SSAS
    -- parent-child hierarchy on Dim_Date (Day -> Month-End -> Quarter-End
    -- -> Year-End -> root). This is an MDX/AMO construct with no direct
    -- Tabular/DAX equivalent (Direct Lake cannot use PATH()-based
    -- calculated columns), used here deliberately to exercise the
    -- migration tool's parent-child detection/reporting.
    ParentDateKey  INT         NULL REFERENCES dbo.Dim_Date(DateKey)
);
GO

DECLARE @StartDate DATE = '2023-01-01';
DECLARE @EndDate   DATE = '2025-12-31';
DECLARE @d DATE = @StartDate;

WHILE @d <= @EndDate
BEGIN
    INSERT INTO dbo.Dim_Date (DateKey, FullDate, [Year], [Quarter], [Month], MonthName, [Day], DayOfWeek, WeekdayName)
    VALUES (
        CONVERT(INT, FORMAT(@d, 'yyyyMMdd')),
        @d,
        YEAR(@d),
        DATEPART(QUARTER, @d),
        MONTH(@d),
        DATENAME(MONTH, @d),
        DAY(@d),
        DATEPART(WEEKDAY, @d),
        DATENAME(WEEKDAY, @d)
    );
    SET @d = DATEADD(DAY, 1, @d);
END
GO

------------------------------------------------------------
-- Populate ParentDateKey (Day -> Month-End -> Quarter-End -> Year-End)
--   * Every ordinary day's parent is the last day of its month.
--   * A month-end that is NOT a quarter-end points to its quarter-end.
--   * A quarter-end that is NOT a year-end (Dec 31) points to Dec 31.
--   * Dec 31 (year-end) is the root of each year's branch (NULL parent).
------------------------------------------------------------
;WITH MonthEnd AS (
    SELECT [Year], [Month], MAX(DateKey) AS MonthEndKey
    FROM dbo.Dim_Date GROUP BY [Year], [Month]
),
QuarterEnd AS (
    SELECT [Year], [Quarter], MAX(DateKey) AS QuarterEndKey
    FROM dbo.Dim_Date GROUP BY [Year], [Quarter]
),
YearEnd AS (
    SELECT [Year], MAX(DateKey) AS YearEndKey
    FROM dbo.Dim_Date GROUP BY [Year]
)
UPDATE d
SET d.ParentDateKey =
    CASE
        WHEN d.DateKey = ye.YearEndKey THEN NULL                 -- root of the year
        WHEN d.DateKey = qe.QuarterEndKey THEN ye.YearEndKey      -- quarter-end -> year-end
        WHEN d.DateKey = me.MonthEndKey THEN qe.QuarterEndKey     -- month-end -> quarter-end
        ELSE me.MonthEndKey                                      -- ordinary day -> month-end
    END
FROM dbo.Dim_Date d
JOIN MonthEnd me ON me.[Year] = d.[Year] AND me.[Month] = d.[Month]
JOIN QuarterEnd qe ON qe.[Year] = d.[Year] AND qe.[Quarter] = d.[Quarter]
JOIN YearEnd ye ON ye.[Year] = d.[Year];
GO

------------------------------------------------------------
-- Dim_Geography (with coordinates)
------------------------------------------------------------
CREATE TABLE dbo.Dim_Geography
(
    GeographyKey INT           NOT NULL IDENTITY(1,1) PRIMARY KEY,
    City         VARCHAR(50)   NOT NULL,
    [State]      VARCHAR(20)   NOT NULL,
    ZipCode      VARCHAR(10)   NOT NULL,
    Region       VARCHAR(20)   NOT NULL,
    Latitude     DECIMAL(9,6)  NOT NULL,
    Longitude    DECIMAL(9,6)  NOT NULL
);
GO

INSERT INTO dbo.Dim_Geography (City, [State], ZipCode, Region, Latitude, Longitude)
VALUES
('Seattle',       'WA', '98101', 'West',      47.606209, -122.332069),
('Portland',      'OR', '97201', 'West',      45.512230, -122.658722),
('San Francisco', 'CA', '94103', 'West',      37.774929, -122.419418),
('Los Angeles',   'CA', '90012', 'West',      34.052235, -118.243683),
('Denver',        'CO', '80202', 'Mountain',  39.739236, -104.990251),
('Phoenix',       'AZ', '85004', 'Mountain',  33.448376, -112.074036),
('Dallas',        'TX', '75201', 'South',     32.776665,  -96.796989),
('Houston',       'TX', '77002', 'South',     29.760427,  -95.369804),
('Atlanta',       'GA', '30303', 'South',     33.749000,  -84.388000),
('Miami',         'FL', '33101', 'South',     25.761681,  -80.191788),
('Chicago',       'IL', '60601', 'Midwest',   41.878113,  -87.629799),
('Minneapolis',   'MN', '55401', 'Midwest',   44.977753,  -93.265015),
('Detroit',       'MI', '48226', 'Midwest',   42.331429,  -83.045753),
('New York',      'NY', '10001', 'Northeast', 40.712776,  -74.005974),
('Boston',        'MA', '02108', 'Northeast', 42.358430,  -71.059773);
GO

------------------------------------------------------------
-- Dim_ClaimType
------------------------------------------------------------
CREATE TABLE dbo.Dim_ClaimType
(
    ClaimTypeKey  INT          NOT NULL IDENTITY(1,1) PRIMARY KEY,
    ClaimTypeName VARCHAR(50)  NOT NULL,
    ClaimCategory VARCHAR(30)  NOT NULL
);
GO

INSERT INTO dbo.Dim_ClaimType (ClaimTypeName, ClaimCategory)
VALUES
('Collision',                       'Physical Damage'),
('Comprehensive',                   'Physical Damage'),
('Liability - Bodily Injury',       'Liability'),
('Liability - Property Damage',     'Liability'),
('Uninsured Motorist',              'Liability'),
('Medical Payments',                'Medical'),
('Personal Injury Protection',      'Medical'),
('Glass / Windshield',              'Physical Damage');
GO

------------------------------------------------------------
-- Dim_Policyholder (20)
------------------------------------------------------------
CREATE TABLE dbo.Dim_Policyholder
(
    PolicyholderKey  INT          NOT NULL IDENTITY(1,1) PRIMARY KEY,
    PolicyholderID   VARCHAR(10)  NOT NULL,
    FirstName        VARCHAR(30)  NOT NULL,
    LastName         VARCHAR(30)  NOT NULL,
    Gender           CHAR(1)      NOT NULL,
    DateOfBirth      DATE         NOT NULL,
    CreditScoreBand  VARCHAR(20)  NOT NULL,
    GeographyKey     INT          NOT NULL REFERENCES dbo.Dim_Geography(GeographyKey)
);
GO

;WITH Names AS (
    SELECT * FROM (VALUES
        ('James','Smith'),('Mary','Johnson'),('Robert','Williams'),('Patricia','Brown'),
        ('John','Jones'),('Jennifer','Garcia'),('Michael','Miller'),('Linda','Davis'),
        ('William','Rodriguez'),('Elizabeth','Martinez'),('David','Hernandez'),('Barbara','Lopez'),
        ('Richard','Gonzalez'),('Susan','Wilson'),('Joseph','Anderson'),('Jessica','Thomas'),
        ('Thomas','Taylor'),('Sarah','Moore'),('Charles','Jackson'),('Karen','Martin')
    ) AS N(FirstName, LastName)
),
Numbered AS (
    SELECT FirstName, LastName, ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS rn
    FROM Names
)
INSERT INTO dbo.Dim_Policyholder (PolicyholderID, FirstName, LastName, Gender, DateOfBirth, CreditScoreBand, GeographyKey)
SELECT
    'PH' + RIGHT('000' + CAST(rn AS VARCHAR(3)), 3),
    FirstName,
    LastName,
    CASE WHEN rn % 2 = 0 THEN 'F' ELSE 'M' END,
    DATEADD(YEAR, -(22 + (rn * 3) % 45), '2025-01-01'),
    CASE (rn % 3) WHEN 0 THEN 'Excellent' WHEN 1 THEN 'Good' ELSE 'Fair' END,
    ((rn - 1) % 15) + 1
FROM Numbered;
GO

------------------------------------------------------------
-- Dim_Policy (50, linked to policyholders)
------------------------------------------------------------
CREATE TABLE dbo.Dim_Policy
(
    PolicyKey        INT           NOT NULL IDENTITY(1,1) PRIMARY KEY,
    PolicyNumber     VARCHAR(15)   NOT NULL,
    PolicyholderKey  INT           NOT NULL REFERENCES dbo.Dim_Policyholder(PolicyholderKey),
    PolicyType       VARCHAR(20)   NOT NULL,
    EffectiveDate    DATE          NOT NULL,
    ExpirationDate   DATE          NOT NULL,
    VehicleMake      VARCHAR(20)   NOT NULL,
    VehicleModel     VARCHAR(20)   NOT NULL,
    VehicleYear      INT           NOT NULL,
    CoverageLimit    DECIMAL(12,2) NOT NULL,
    DeductibleAmount DECIMAL(10,2) NOT NULL
);
GO

;WITH Vehicles AS (
    SELECT * FROM (VALUES
        ('Toyota','Camry'),('Honda','Accord'),('Ford','F-150'),('Chevrolet','Silverado'),
        ('Toyota','RAV4'),('Honda','CR-V'),('Nissan','Altima'),('Jeep','Grand Cherokee'),
        ('Subaru','Outback'),('Tesla','Model 3'),('Ford','Explorer'),('Hyundai','Elantra'),
        ('Kia','Sportage'),('Chevrolet','Equinox'),('BMW','3 Series')
    ) AS V(Make, Model)
),
NumberedVeh AS (
    SELECT Make, Model, ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS rn FROM Vehicles
),
Seq AS (
    SELECT TOP 50 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n
    FROM sys.all_objects
)
INSERT INTO dbo.Dim_Policy (PolicyNumber, PolicyholderKey, PolicyType, EffectiveDate, ExpirationDate,
                            VehicleMake, VehicleModel, VehicleYear, CoverageLimit, DeductibleAmount)
SELECT
    'POL' + RIGHT('0000' + CAST(n AS VARCHAR(4)), 4),
    ((n - 1) % 20) + 1,
    CASE (n % 3) WHEN 0 THEN 'Full Coverage' WHEN 1 THEN 'Liability Only' ELSE 'Collision Only' END,
    DATEADD(MONTH, -((n * 2) % 36), '2025-06-01'),
    DATEADD(MONTH, 12 - ((n * 2) % 36), '2025-06-01'),
    v.Make,
    v.Model,
    2015 + (n % 10),
    CASE (n % 3) WHEN 0 THEN 100000.00 WHEN 1 THEN 50000.00 ELSE 25000.00 END,
    CASE (n % 4) WHEN 0 THEN 250.00 WHEN 1 THEN 500.00 WHEN 2 THEN 1000.00 ELSE 2000.00 END
FROM Seq
JOIN NumberedVeh v ON v.rn = ((n - 1) % 15) + 1;
GO

------------------------------------------------------------
-- Fact_Claims (~100 claims over 3 years)
------------------------------------------------------------
CREATE TABLE dbo.Fact_Claims
(
    ClaimKey          INT           NOT NULL IDENTITY(1,1) PRIMARY KEY,
    ClaimNumber       VARCHAR(15)   NOT NULL,
    PolicyKey         INT           NOT NULL REFERENCES dbo.Dim_Policy(PolicyKey),
    PolicyholderKey   INT           NOT NULL REFERENCES dbo.Dim_Policyholder(PolicyholderKey),
    ClaimTypeKey      INT           NOT NULL REFERENCES dbo.Dim_ClaimType(ClaimTypeKey),
    GeographyKey      INT           NOT NULL REFERENCES dbo.Dim_Geography(GeographyKey),
    DateOfLossKey     INT           NOT NULL REFERENCES dbo.Dim_Date(DateKey),
    DateReportedKey   INT           NOT NULL REFERENCES dbo.Dim_Date(DateKey),
    ClaimStatus       VARCHAR(15)   NOT NULL,
    IncurredAmount    DECIMAL(12,2) NOT NULL,
    PaidAmount        DECIMAL(12,2) NOT NULL,
    ReserveAmount     DECIMAL(12,2) NOT NULL,
    DeductibleAmount  DECIMAL(10,2) NOT NULL,
    ClaimCount        INT           NOT NULL DEFAULT 1
);
GO

;WITH Seq AS (
    SELECT TOP 100 ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS n
    FROM sys.all_objects
),
Calc AS (
    SELECT
        n,
        ((n - 1) % 50) + 1                                          AS PolicyKey,
        ((n * 7) % 8) + 1                                           AS ClaimTypeKey,
        ((n * 5) % 15) + 1                                          AS GeographyKeyOverride,
        DATEADD(DAY, (n * 11) % 1080, '2023-01-01')                 AS LossDate,
        CASE (n % 3) WHEN 0 THEN 'Closed' WHEN 1 THEN 'Open' ELSE 'Closed' END AS ClaimStatus,
        1500.00 + ((n * 137) % 12000)                               AS IncurredAmount,
        CASE (n % 4) WHEN 3 THEN 0 ELSE (n * 91) % 500 END          AS DeductibleAmount
    FROM Seq
)
INSERT INTO dbo.Fact_Claims (ClaimNumber, PolicyKey, PolicyholderKey, ClaimTypeKey, GeographyKey,
                             DateOfLossKey, DateReportedKey, ClaimStatus,
                             IncurredAmount, PaidAmount, ReserveAmount, DeductibleAmount, ClaimCount)
SELECT
    'CLM' + RIGHT('00000' + CAST(c.n AS VARCHAR(5)), 5),
    c.PolicyKey,
    p.PolicyholderKey,
    c.ClaimTypeKey,
    g.GeographyKey,
    CONVERT(INT, FORMAT(c.LossDate, 'yyyyMMdd')),
    CONVERT(INT, FORMAT(DATEADD(DAY, 1 + (c.n % 10), c.LossDate), 'yyyyMMdd')),
    c.ClaimStatus,
    c.IncurredAmount,
    CASE c.ClaimStatus WHEN 'Closed' THEN c.IncurredAmount - c.DeductibleAmount
                        ELSE ROUND(c.IncurredAmount * 0.4, 2) END,
    CASE c.ClaimStatus WHEN 'Closed' THEN 0
                        ELSE ROUND(c.IncurredAmount * 0.6, 2) END,
    c.DeductibleAmount,
    1
FROM Calc c
JOIN dbo.Dim_Policy p ON p.PolicyKey = c.PolicyKey
JOIN dbo.Dim_Geography g ON g.GeographyKey = c.GeographyKeyOverride;
GO

------------------------------------------------------------
-- Verification counts
------------------------------------------------------------
SELECT 'Dim_Date' AS TableName, COUNT(*) AS [RowCount] FROM dbo.Dim_Date
UNION ALL SELECT 'Dim_Geography', COUNT(*) FROM dbo.Dim_Geography
UNION ALL SELECT 'Dim_ClaimType', COUNT(*) FROM dbo.Dim_ClaimType
UNION ALL SELECT 'Dim_Policyholder', COUNT(*) FROM dbo.Dim_Policyholder
UNION ALL SELECT 'Dim_Policy', COUNT(*) FROM dbo.Dim_Policy
UNION ALL SELECT 'Fact_Claims', COUNT(*) FROM dbo.Fact_Claims;
GO

using System.Text.Json.Serialization;

namespace StudentFeePoc.Models;

public sealed record FeeTransactionApiResponse(
    [property: JsonPropertyName("data")] IReadOnlyList<FeeTransactionDto> Data
);

public sealed record FeeTransactionDto(
    [property: JsonPropertyName("id")] Guid Id,
    [property: JsonPropertyName("ownerId")] Guid? OwnerId,
    [property: JsonPropertyName("parentId")] Guid? ParentId,
    [property: JsonPropertyName("txNo")] string? TxNo,
    [property: JsonPropertyName("txDate")] string? TxDate,
    [property: JsonPropertyName("studentId")] Guid? StudentId,
    [property: JsonPropertyName("studentName")] string? StudentName,
    [property: JsonPropertyName("courseId")] string? CourseId,
    [property: JsonPropertyName("courseName")] string? CourseName,
    [property: JsonPropertyName("section")] string? Section,
    [property: JsonPropertyName("amount")] decimal Amount,
    [property: JsonPropertyName("installmentsPaid")] IReadOnlyList<InstallmentPaidDto> InstallmentsPaid,
    [property: JsonPropertyName("finesPaid")] IReadOnlyList<object> FinesPaid,
    [property: JsonPropertyName("discounts")] IReadOnlyList<object> Discounts,
    [property: JsonPropertyName("feeAdjustment")] FeeAdjustmentDto FeeAdjustment,
    [property: JsonPropertyName("isFinePaid")] bool IsFinePaid,
    [property: JsonPropertyName("isDiscountGiven")] bool IsDiscountGiven,
    [property: JsonPropertyName("isOpeningBalanceAdjusted")] bool IsOpeningBalanceAdjusted,
    [property: JsonPropertyName("status")] int Status,
    [property: JsonPropertyName("refNo")] string? RefNo,
    [property: JsonPropertyName("paidBy")] string? PaidBy
);

public sealed record InstallmentPaidDto(
    [property: JsonPropertyName("feeId")] Guid? FeeId,
    [property: JsonPropertyName("feeName")] string? FeeName,
    [property: JsonPropertyName("feeDisplayText")] string? FeeDisplayText,
    [property: JsonPropertyName("installmentId")] int? InstallmentId,
    [property: JsonPropertyName("description")] string? Description,
    [property: JsonPropertyName("dueDate")] string? DueDate,
    [property: JsonPropertyName("amount")] decimal Amount
);

public sealed record FeeAdjustmentDto(
    [property: JsonPropertyName("id")] Guid Id,
    [property: JsonPropertyName("name")] string? Name,
    [property: JsonPropertyName("description")] string? Description,
    [property: JsonPropertyName("amount")] decimal Amount
);

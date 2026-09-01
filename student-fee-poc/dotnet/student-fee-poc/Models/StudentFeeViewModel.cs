using System.Text.Json;
using System.Text.Json.Serialization;

namespace StudentFeePoc.Models;

public sealed record StudentFeeViewModel(
    [property: JsonPropertyName("id")] Guid Id,
    [property: JsonPropertyName("ownerId")] Guid? OwnerId,
    [property: JsonPropertyName("parentId")] Guid? ParentId,
    [property: JsonPropertyName("txNo")] string? TxNo,
    [property: JsonPropertyName("txDate")] string? TxDate,
    [property: JsonPropertyName("studentId")] Guid? StudentId,
    [property: JsonPropertyName("studentCode")] string? StudentCode,
    [property: JsonPropertyName("studentName")] string? StudentName,
    [property: JsonPropertyName("courseId")] string? CourseId,
    [property: JsonPropertyName("courseName")] string? CourseName,
    [property: JsonPropertyName("section")] string? Section,
    [property: JsonPropertyName("termName")] string? TermName,
    [property: JsonPropertyName("amount")] decimal Amount,
    [property: JsonPropertyName("feeId")] Guid? FeeId,
    [property: JsonPropertyName("feeName")] string? FeeName,
    [property: JsonPropertyName("feeDisplayText")] string? FeeDisplayText,
    [property: JsonPropertyName("installmentId")] int? InstallmentId,
    [property: JsonPropertyName("installmentDescription")] string? InstallmentDescription,
    [property: JsonPropertyName("dueDate")] string? DueDate,
    [property: JsonPropertyName("paidAmount")] decimal PaidAmount,
    [property: JsonPropertyName("finesPaid")] JsonElement FinesPaid,
    [property: JsonPropertyName("discounts")] JsonElement Discounts,
    [property: JsonPropertyName("feeAdjustment")] JsonElement FeeAdjustment,
    [property: JsonPropertyName("isFinePaid")] bool IsFinePaid,
    [property: JsonPropertyName("isDiscountGiven")] bool IsDiscountGiven,
    [property: JsonPropertyName("isOpeningBalanceAdjusted")] bool IsOpeningBalanceAdjusted,
    [property: JsonPropertyName("status")] string? Status,
    [property: JsonPropertyName("statusCode")] int StatusCode,
    [property: JsonPropertyName("refNo")] string? RefNo,
    [property: JsonPropertyName("paidBy")] string? PaidBy
);

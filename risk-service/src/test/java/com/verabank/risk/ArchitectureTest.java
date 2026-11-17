package com.verabank.risk;

import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;

import com.tngtech.archunit.core.importer.ImportOption;
import com.tngtech.archunit.junit.AnalyzeClasses;
import com.tngtech.archunit.junit.ArchTest;
import com.tngtech.archunit.lang.ArchRule;

/** ADR-08: dos roles en un solo servicio exigen disciplina de paquetes para poder separarlos después. */
@AnalyzeClasses(packages = "com.verabank.risk", importOptions = ImportOption.DoNotIncludeTests.class)
class ArchitectureTest {

    @ArchTest
    static final ArchRule scoringDoesNotImportStream = noClasses().that().resideInAPackage("..risk.scoring..")
            .should().dependOnClassesThat().resideInAPackage("..risk.stream..");

    @ArchTest
    static final ArchRule streamDoesNotImportScoring = noClasses().that().resideInAPackage("..risk.stream..")
            .should().dependOnClassesThat().resideInAPackage("..risk.scoring..");

    @ArchTest
    static final ArchRule sharedDoesNotDependOnARole = noClasses().that().resideInAPackage("..risk.shared..")
            .should().dependOnClassesThat().resideInAnyPackage("..risk.scoring..", "..risk.stream..");
}
